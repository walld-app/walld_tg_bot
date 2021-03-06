"""
Main bot module
"""
import json
from threading import Thread
from time import sleep

import telebot
from walld_db.helpers import DB, Rmq
from walld_db.models import (Admin, AdminStates, Category, Moderator,
                             ModStates, RejectedPicture, SubCategory, Tag,
                             User)

from config import (DB_HOST, DB_NAME, DB_PASSWORD, DB_PORT, DB_USER, RMQ_HOST,
                    RMQ_PASS, RMQ_PORT, RMQ_USER, TG_TOKEN, log)
from helpers import gen_inline_markup, gen_markup, prepare_json_review, has_cyrillic
from meta import Answers

# logging.basicConfig(level=logging.INFO)

bot = telebot.TeleBot(TG_TOKEN)

# TODO make server and polling method

bot.delete_webhook()

rmq = Rmq(host=RMQ_HOST, port=RMQ_PORT, user=RMQ_USER, passw=RMQ_PASS)
db = DB(user=DB_USER, passwd=DB_PASSWORD, host=DB_HOST, port=DB_PORT, name=DB_NAME)


@bot.message_handler(commands=['start'])
def pass_start(m):
    """
    this is a stealth bot, we need to ignore start
    """
    pass


@bot.message_handler(commands=['reset'])
def reset_user(message):
    """
    Reset mod state to available
    """
    with db.get_session() as ses:
        user = ses.query(User, Moderator).join(Moderator).\
               filter(User.telegram_id == message.chat.id).one()

        user.Moderator.tg_state = ModStates.available
    bot.send_message(message.chat.id, 'ok!')


@bot.callback_query_handler(func=lambda call: True)
def do_stuff(call):
    """
    На присланной картинке поставлены две кнопки да и нет
    Эта функция обрабатывает нажим кнопок
    По сути 2 шаг по обработке картинки
    """
    with db.get_session() as ses:  # TODO long session
        dude = ses.query(User, Moderator).filter(Moderator.id == User.id, User.telegram_id == call.from_user.id)
        dude = dude.one_or_none()

        if not dude:
            return

        pic_json = dude.Moderator.json_review
        last_message = dude.Moderator.last_message

        if call.data == 'cb_yes' or call.data == 'done_no':
            dude.Moderator.tg_state = ModStates.choosing_category

            bot.answer_callback_query(call.id, "Погнали")
            categories = db.categories
            categories.append('Добавить новую...')
            bot.edit_message_reply_markup(dude.User.telegram_id, message_id=last_message)
            bot.send_message(call.from_user.id, 'Категория!', reply_markup=gen_markup(categories))

        elif call.data == 'cb_no':
            bot.edit_message_reply_markup(dude.User.telegram_id, message_id=last_message)
            bot.answer_callback_query(call.id, "Забываем про пикчу")
            dude.Moderator.tg_state = ModStates.available

            rejected_pic = RejectedPicture(mod_id=dude.Moderator.id,
                                           uploader='Pexels crawler',
                                           url=pic_json['download_url'])
            ses.add(rejected_pic)

        elif call.data == 'done_yes':
            bot.edit_message_reply_markup(dude.User.telegram_id, message_id=last_message)
            bot.answer_callback_query(call.id, "Спасибо! Бросил на обработку")

            rmq.channel.basic_publish(exchange='',
                                      routing_key='go_sql',
                                      properties=rmq.durable,
                                      body=json.dumps(pic_json))

            pic_json['mod_review_id'] = dude.User.id

            dude.Moderator.pics_accepted += 1
            dude.Moderator.tg_state = ModStates.available

            bot.send_message(call.from_user.id, 'ok', reply_markup=gen_markup())


@bot.message_handler(commands=['reg'])
def cmd_reg(message):
    """
    Регестрирует пользователя
    и если нет пользователей вообще
    делает его админом
    """
    with db.get_session() as ses:
        admin_presented = ses.query(Admin).one_or_none()
        dude = ses.query(User).filter_by(telegram_id=message.chat.id).one_or_none()

        if not dude:
            nick = message.chat.username or 'No_nickname'
            dude = User(name=nick, telegram_id=message.chat.id)
            ses.add(dude)
            ses.commit()
            bot.send_message(message.chat.id, 'Regged!')

            if not admin_presented:
                ses.add(Admin(user_id=dude.id))
                ses.add(Moderator(user_id=dude.id))

        else:
            bot.send_message(message.chat.id, 'Already!')


@bot.message_handler(commands=['raise_user'])
def raise_user(message):
    """
    Чисто админский метод
    С помощью него можно добавить
    Юзера в модераторы т.е. дать возможность чекать картинки
    выдаем клавиатуру со всеми известными юзерами
    """
    with db.get_session() as ses:
        dude = ses.query(User,
                         Admin).filter(User.telegram_id == message.chat.id, User.id == Admin.user_id).one_or_none()
        if dude:
            dudes = db.users
            bot.send_message(message.chat.id, 'which one?', reply_markup=gen_markup(dudes))

            dude.Admin.tg_state = AdminStates.raising_user


@bot.message_handler(func=lambda m: db.get_state(m.chat.id, Admin) == AdminStates.raising_user)
def raise_user_step_two(message):
    """`
    Обработка второго шага повышение привелегий юзера
    """
    with db.get_session() as ses:
        user = ses.query(User).filter_by(nickname=message.text).one()

        if user:
            ses.add(Moderator(user_id=user.id))
            bot.send_message(message.chat.id, Answers.ok)

            admin = ses.query(User, Admin).filter_by(telegram_id=message.chat.id).one()
            admin.Admin.tg_state = AdminStates.available

        else:
            bot.send_message(message.chat.id, 'not found user')


@bot.message_handler(func=lambda m: db.get_state(m.chat.id, Moderator) == ModStates.choosing_category)
def apply_category(message):
    """
    Обрабатываем 2 шаг
    выдаем 3 стадию если нет необходимой категории
    выдаем 4 стадию если желаемая категория существует
    """
    with db.get_session() as ses:
        query = ses.query(User, Moderator).join(Moderator, User.id == Moderator.user_id)
        user = query.filter(User.telegram_id == message.chat.id).one()

        if message.text in db.categories:
            user.Moderator.json_review['category'] = message.text
            user.Moderator.tg_state = ModStates.choosing_sub_category
            category = db.get_row(Category, session=ses, name=message.text)
            sub_cats = list(category.sub_categories)

            if category.sub_categories:
                sub_cats = [i.name for i in sub_cats]

            sub_cats.append(Answers.add_new)

            bot.send_message(message.chat.id, 'Неплохо, далее под_категория', reply_markup=gen_markup(sub_cats))

        elif message.text == Answers.add_new:
            user.Moderator.tg_state = ModStates.making_category
            bot.send_message(message.chat.id,
                             'Окей, дай мне название категории',
                             reply_markup=gen_markup())

        else:
            bot.send_message(message.chat.id, ('Ты находишься в состоянии '
                                               'подбора категории, кнопки '
                                               'доступны в клавиатуре'))


@bot.message_handler(func=lambda m: db.get_state(m.chat.id, Moderator) == ModStates.choosing_sub_category)
def apply_sub_category(message):
    """
    Обработаем 4 стадию
    Выдаем 5 стадию если нужной подкатегории нет
    Выдаем финальную 6 стадию если есть
    """
    with db.get_session() as ses:
        user = db.get_moderator(message.chat.id, session=ses)
        cat = user.Moderator.json_review['category']
        category = db.get_row(Category, session=ses, name=cat)
        sub_cats = [i.name for i in category.sub_categories]

        if message.text in sub_cats:
            user.Moderator.json_review['sub_category'] = message.text
            user.Moderator.tg_state = ModStates.choosing_tags

            tags = db.named_tags
            tags.append(Answers.add_new)
            tags.append(Answers.ok)

            bot.send_message(message.chat.id, 'Тэги!', reply_markup=gen_markup(tags))

        elif message.text == Answers.add_new:
            user.Moderator.tg_state = ModStates.making_sub_category
            bot.send_message(message.chat.id,
                             'Окей, дай мне название подкатегории',
                             reply_markup=gen_markup())

        else:
            bot.send_message(message.chat.id, ('Ты находишься в состоянии '
                                               'подбора подкатегории, кнопки '
                                               'доступны в клавиатуре'))


@bot.message_handler(func=lambda m: db.get_state(m.chat.id, Moderator) == ModStates.choosing_tags)
def choose_tag(message):
    """
    Выбираем тэги тут
    """

    with db.get_session() as ses:
        user = db.get_moderator(message.chat.id, session=ses)
        tag = db.get_row(Tag, session=ses, name=message.text)
        text = message.text

        if not user.Moderator.json_review.get('tags'):
            user.Moderator.json_review['tags'] = []
        pic_tags = user.Moderator.json_review['tags']

        if text == Answers.ok:
            body = prepare_json_review(user.Moderator.json_review)
            r_markup = gen_inline_markup(cb_yes='done_yes', cb_no='done_no')

            review = bot.send_message(message.chat.id, body, reply_markup=r_markup)
            user.Moderator.last_message = review.message_id

        elif text == Answers.add_new:
            bot.send_message(message.chat.id,
                             "Добавим новый тэг, введи название",
                             reply_markup=gen_markup())
            user.Moderator.tg_state = ModStates.making_tags

        elif text in db.named_tags and tag.name not in pic_tags:
            pic_tags.append(tag.name)
            user.Moderator.json_review['tags'] = pic_tags
            bot.send_message(message.chat.id, Answers.ok)

        elif tag is not None and tag.name in pic_tags:
            pic_tags.remove(tag.name)
            user.Moderator.json_review['tags'] = pic_tags
            bot.send_message(message.chat.id, Answers.deleted)

        else:
            bot.send_message(message.chat.id, 'Ты подбираешь тэги, если что')


def has_cyrillic_or_space(message: str):  # TODO bad naming
    if has_cyrillic(message) or ' ' in message:
        return True
    return False


# TODO decorator?
@bot.message_handler(func=lambda m: db.get_state(m.chat.id, Moderator) == ModStates.making_tags)
def create_tag(message):
    text = message.text

    if text in [Answers.ok, Answers.add_new] or has_cyrillic_or_space(text):
        bot.send_message(message.chat.id, 'Не ошибся ли?')
        return

    with db.get_session() as ses:
        ses.add(Tag(name=text))
        user = db.get_moderator(message.chat.id, session=ses)
        user.Moderator.tg_state = ModStates.choosing_tags

    tags = db.named_tags
    tags.append(Answers.add_new)
    tags.append(Answers.ok)

    bot.send_message(user.User.telegram_id, Answers.done, reply_markup=gen_markup(tags))


@bot.message_handler(func=lambda m: db.get_state(m.chat.id, Moderator) == ModStates.making_sub_category)
def create_sub_category(message):
    """
    Создаем подкатегорию в категории
    """
    text = message.text
    if text in [Answers.add_new, Answers.ok] or has_cyrillic_or_space(text):
        bot.send_message(message.chat.id, 'Не ошибся ли?')
        return

    with db.get_session() as ses:
        user = db.get_moderator(message.chat.id, session=ses)
        category = user.Moderator.json_review['category']

        category = db.get_row(Category, session=ses, name=category)
        ses.add(SubCategory(category_id=category.id, name=text))
        user.Moderator.tg_state = ModStates.choosing_sub_category

    sub_cats = [i.name for i in category.sub_categories]
    sub_cats.append(text)
    sub_cats.append(Answers.add_new)

    bot.send_message(message.chat.id, Answers.done, reply_markup=gen_markup(sub_cats))


@bot.message_handler(func=lambda m: db.get_state(m.chat.id, Moderator) == ModStates.making_category)
def create_category(message):
    text = message.text

    if text in [Answers.add_new, Answers.ok] or has_cyrillic_or_space(text):
        bot.send_message(message.chat.id, 'Не ошибся ли?')
        return

    with db.get_session() as ses:
        ses.add(Category(name=text))
        user = db.get_moderator(message.chat.id, session=ses)
        user.Moderator.tg_state = ModStates.choosing_category

    categories = db.categories
    categories.append(Answers.add_new)

    bot.send_message(user.User.telegram_id, Answers.done, reply_markup=gen_markup(categories))


def send_pics_to_mods():
    """
    Присылаем всем модераторам у которых статус
    Available картинку на оценку
    """
    while True:
        with db.get_session() as ses:
            mods = ses.query(User, Moderator).join(User, User.id == Moderator.user_id)
            avail_mods = mods.filter(Moderator.tg_state == ModStates.available)

            for mod in avail_mods:
                body = rmq.get_message(1, queue_name='check_out').decode()
                body = json.loads(body)
                text = ("Новая пикча!\n"
                        f"Разрешение - {body['width']}x{body['height']}\n"
                        f"Сервис - {body['service']}\n"
                        f'Превью - \n{body["preview_url"]}\n'
                        f'Оригинал - \n{body["download_url"]}\n')

                message = bot.send_message(mod.User.telegram_id, text, reply_markup=gen_inline_markup())

                mod.Moderator.tg_state = ModStates.got_picture
                mod.Moderator.last_message = message.message_id
                mod.Moderator.json_review = body

        rmq.connection.process_data_events()
        sleep(1.5)


def main(pics=False, updates=False):
    """
    Main function that starts all threads
    pass true to variable pics for sending pics thread
    pass true for updates to start polling updates
    """
    pol_updates = Thread(target=bot.polling)
    send_pics = Thread(target=send_pics_to_mods)

    if pics:
        send_pics.start()
    if updates:
        pol_updates.start()


if __name__ == '__main__':
    main(pics=True, updates=True)
