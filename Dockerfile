
FROM python:3.8.2-alpine3.11

RUN apk update && apk add gcc postgresql-dev python3-dev musl-dev git --no-cache
COPY requirements.txt .
RUN pip3 install -r requirements.txt
WORKDIR /app
COPY . .
CMD python3 walld_tg_bot/bot.py
