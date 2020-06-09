"""
Main bot module
"""
import json
from threading import Thread
from time import sleep

import telebot
from walld_db.models import (Admin, AdminStates, Category, Moderator,
                             ModStates, SubCategory, Tag, User)

from config import (DB_HOST, DB_NAME, DB_PASSWORD, DB_PORT, DB_USER, RMQ_HOST,
                    RMQ_PASS, RMQ_PORT, RMQ_USER, TG_TOKEN)
from helpers import DB, Rmq, gen_inline_markup, gen_markup, prepare_json_review
from meta import Answers
from sqlalchemy.orm import joinedload

#logging.basicConfig(level=logging.INFO)

telebot.apihelper.proxy = {'https':'socks5://127.0.0.1:8123'}
bot = telebot.TeleBot(TG_TOKEN)

rmq = Rmq(host=RMQ_HOST,
          port=RMQ_PORT,
          user=RMQ_USER,
          passw=RMQ_PASS)

db = DB(db_user=DB_USER,
        db_passwd=DB_PASSWORD,
        db_host=DB_HOST,
        db_port=DB_PORT,
        db_name=DB_NAME)

@bot.message_handler(commands=['start'])
def pass_start(m):
    pass

@bot.message_handler(commands=['reset'])
def reset_user(message):
    with db.get_session() as ses:
        user = ses.query(User, Moderator).join(Moderator).\
               filter(User.telegram_id == message.chat.id).one()

        user.Moderator.tg_state = ModStates.available


@bot.callback_query_handler(func=lambda call: True)
def do_stuff(call):
    """
    На присланной картинке поставлены две кнопки да и нет
    Эта функция обрабатывает нажим кнопок
    По сути 2 шаг по обработке картинки
    """
    with db.get_session() as ses:
        dude = ses.query(User, Moderator).filter(Moderator.user_id == User.user_id,
                                                 User.telegram_id == call.from_user.id)
        dude = dude.one()

        if (call.data == 'cb_yes' or call.data == 'done_no'):
            dude.Moderator.tg_state = ModStates.choosing_category
            bot.answer_callback_query(call.id, "Погнали")
            categories = db.categories
            categories.append('Добавить новую...')
            bot.edit_message_reply_markup(dude.User.telegram_id,
                                          message_id=dude.Moderator.last_message)
            bot.send_message(call.from_user.id,
                             'Категория!',
                             reply_markup=gen_markup(categories))

        elif call.data == 'cb_no':
            bot.edit_message_reply_markup(dude.User.telegram_id,
                                        message_id=dude.Moderator.last_message)
            bot.answer_callback_query(call.id, "Забываем про пикчу")
            dude.Moderator.tg_state = ModStates.available
            # TODO make rejected pictures table

        elif call.data == 'done_yes':
            bot.edit_message_reply_markup(dude.User.telegram_id,
                                          message_id=dude.Moderator.last_message)
            bot.answer_callback_query(call.id, "Спасибо! Бросил на обработку")
            dude.Moderator.pics_accepted += 1
            dude.Moderator.json_review['mod_review_id'] = dude.User.user_id
            # TODO StreamLostError indicated EOF
            rmq.send_message(str(dude.Moderator.json_review))
            bot.send_message(call.from_user.id, 'ok', reply_markup=gen_markup())
            dude.Moderator.tg_state = ModStates.available

@bot.message_handler(commands=['reg'])
def cmd_reg(message):
    """
    Регестрирует пользователя
    и если нет пользователей вообще
    делает его админом
    """
    with db.get_session() as ses:
        is_there_admins = ses.query(Admin).one_or_none()
        dude = ses.query(User).\
               filter_by(telegram_id=message.chat.id).one_or_none()
        if not dude:
            nick = message.chat.username or 'No_nickname'
            dude = User(nickname=nick,
                        telegram_id=message.chat.id)
            ses.add(dude)
            ses.commit()
            bot.send_message(message.chat.id, 'Regged!')
            if not is_there_admins:
                ses.add(Admin(user_id=dude.user_id))
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
        dude = ses.query(User, Admin).filter(User.telegram_id == message.chat.id,
                                             User.user_id==Admin.user_id).one_or_none()
        if dude:
            dudes = db.users
            bot.send_message(message.chat.id,
                             'which one?',
                             reply_markup=gen_markup(dudes))
            dude.Admin.tg_state = AdminStates.raising_user

@bot.message_handler(func=lambda m: db.get_state(m.chat.id, Admin) == AdminStates.raising_user)
def raise_user_step_two(message):
    """
    Обработка второго шага повышение привелегий юзера
    """
    with db.get_session() as ses:
        user = ses.query(User).filter_by(nickname=message.text).one_or_none()
        if user:
            ses.add(Moderator(user_id=user.user_id))
            bot.send_message(message.chat.id, Answers.ok)
        else:
            bot.send_message(message.chat.id, 'not found user')
        admin = ses.query(User, Admin).filter_by(telegram_id=message.chat.id).one()
        admin.Admin.tg_state = AdminStates.available

@bot.message_handler(func=lambda m: db.get_state(m.chat.id, Moderator) == ModStates.choosing_category)
def apply_category(message):
    """
    Обрабатываем 2 шаг
    выдаем 3 стадию если нет необходимой категории
    выдаем 4 стадию если желаемая категория существует
    """
    with db.get_session() as ses: 
        # TODO Очень много with session,
        # мб есть прикол чтоб запихнуть это в декоратор?
        user = ses.query(User, Moderator).\
               join(Moderator, User.user_id == Moderator.user_id).\
               filter(User.telegram_id==message.chat.id).one()
        if message.text in db.categories:
            user.Moderator.json_review['category'] = message.text
            user.Moderator.tg_state = ModStates.choosing_sub_category
            sub_cats = db.get_sub_categories(message.text)
            sub_cats.append(Answers.add_new)
            bot.send_message(message.chat.id,
                             'Неплохо, далее под_категория',
                             reply_markup=gen_markup(sub_cats))

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

        if message.text in db.get_sub_categories(cat):
            user.Moderator.json_review['sub_category'] = message.text
            user.Moderator.tg_state = ModStates.choosing_tags
            tags = db.tags
            tags.append(Answers.add_new)
            tags.append(Answers.ok)
            bot.send_message(message.chat.id,
                             'Тэги!',
                             reply_markup=gen_markup(tags))

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
    TODO Сделать эмодзи выбранного тэга
    """

    with db.get_session() as ses:
        user = db.get_moderator(message.chat.id, session=ses)

        if not user.Moderator.json_review.get('tags'):
            user.Moderator.json_review['tags'] = []
        pic_tags = user.Moderator.json_review['tags']
        if (message.text in db.tags and message.text not in pic_tags):
            pic_tags.append(message.text)
            user.Moderator.json_review['tags'] = pic_tags
            bot.send_message(message.chat.id, Answers.ok)

        elif message.text in pic_tags:
            pic_tags.remove(message.text)
            user.Moderator.json_review['tags'] = pic_tags
            bot.send_message(message.chat.id, Answers.deleted)

        elif message.text == Answers.add_new:
            bot.send_message(message.chat.id,
                             "Добавим новый тэг, введи название",
                             reply_markup=gen_markup())
            user.Moderator.tg_state = ModStates.making_tags

        elif message.text == Answers.ok:
            review = bot.send_message(message.chat.id,
                                      prepare_json_review(user.Moderator.json_review),
                                      reply_markup=gen_inline_markup(cb_yes='done_yes',
                                                                     cb_no='done_no'))
            user.Moderator.last_message = review.message_id

@bot.message_handler(func=lambda m: db.get_state(m.chat.id, Moderator) == ModStates.making_tags)
def create_tag(message):
    if (message.text == Answers.add_new or message.text == Answers.ok):
        bot.send_message(message.chat.id, 'Не ошибся ли?')
        return
    with db.get_session() as ses:
        ses.add(Tag(tag_name=message.text))
        user = db.get_moderator(message.chat.id, session=ses)
        user.Moderator.tg_state = ModStates.choosing_tags
    tags = db.tags
    tags.append(Answers.add_new)
    tags.append(Answers.ok)
    bot.send_message(user.User.telegram_id,
                     Answers.done,
                     reply_markup=gen_markup(tags))

@bot.message_handler(func=lambda m: db.get_state(m.chat.id, Moderator) == ModStates.making_sub_category)
def create_sub_category(message):
    if message.text == Answers.add_new:
        bot.send_message(message.chat.id, 'Не ошибся ли?')
        return
    with db.get_session() as ses:
        user = db.get_moderator(message.chat.id, session=ses)
        category = user.Moderator.json_review['category']
        cat_id = db.get_category(category).category_id
        ses.add(SubCategory(category_id=cat_id,
                            sub_category_name=message.text))
        user.Moderator.tg_state = ModStates.choosing_sub_category

    sub_cats = db.get_sub_categories(category)
    sub_cats.append(Answers.add_new)
    bot.send_message(message.chat.id,
                     Answers.done, 
                     reply_markup=gen_markup(sub_cats))


@bot.message_handler(func=lambda m: db.get_state(m.chat.id, Moderator) == ModStates.making_category)
def create_category(message):
    if message.text == Answers.add_new:
        bot.send_message(message.chat.id, 'Не ошибся ли?')
        return

    with db.get_session() as ses:
        ses.add(Category(category_name=message.text))
        user = db.get_moderator(message.chat.id, session=ses)
        user.Moderator.tg_state = ModStates.choosing_category

    categories = db.categories
    categories.append(Answers.add_new)
    bot.send_message(user.User.telegram_id,
                     Answers.done,
                     reply_markup=gen_markup(categories))

def send_pics_to_mods():
    """
    Присылаем всем модераторам у которых статус
    Available картинку на оценку
    """
    while True:
        with db.get_session() as ses:
            avail_mods = ses.query(User, Moderator).\
                         join(User, User.user_id==Moderator.user_id).\
                         filter(Moderator.tg_state == ModStates.available)
            for user in avail_mods:
                # TODO body can crash with stream indicated EOF
                body = json.loads(rmq.get_message(1).decode())
                text = ("Новая пикча!\n"
                        f"Разрешение - {body['width']}x{body['height']}\n"
                        f"Сервис - {body['service']}\n"
                        f'Превью - \n{body["preview_url"]}\n'
                        f'Оригинал - \n{body["download_url"]}\n')
                message = bot.send_message(user.User.telegram_id,
                                           text,
                                           reply_markup=gen_inline_markup())
                user.Moderator.tg_state = ModStates.got_picture
                user.Moderator.last_message = message.message_id
                user.Moderator.json_review = body
        sleep(3)

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
