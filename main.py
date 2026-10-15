import asyncio
import os
from aiogram import Bot, Dispatcher, F, types
from aiogram.enums import ParseMode

# === НАСТРОЙКИ ===
BOT_TOKEN = "ВАШ_ТОКЕН_ИЗ_BOTFATHER"
# Введите ваш личный Telegram ID (чтобы бот знал, куда отправлять удаленные медиа)
# Свой ID можно узнать через бота @userinfobot
OWNER_ID = 123456789

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Создаем папку для временного хранения медиа
MEDIA_DIR = "cached_media"
os.makedirs(MEDIA_DIR, exist_ok=True)

# Локальная база данных в памяти: { message_id: { 'text': ..., 'file_path': ..., 'user': ... } }
db = {}


@dp.business_message()
async def handle_business_message(message: types.Message):
  """Обрабатывает и кэширует входящие бизнес-сообщения."""
  msg_id = message.message_id
  chat_id = message.chat.id
  sender = message.from_user

  sender_info = (
      f"{sender.first_name} {sender.last_name or ''} (@{sender.username or 'нет'})"
  )

  data = {
      'text': message.text or message.caption or '',
      'sender_info': sender_info,
      'chat_id': chat_id,
      'file_path': None,
      'type': 'text',
  }

  # Если прислали фото
  if message.photo:
    photo = message.photo[-1]  # Берем самое высокое качество
    file_path = os.path.join(MEDIA_DIR, f"{chat_id}_{msg_id}.jpg")
    await bot.download(photo.file_id, destination=file_path)
    data['file_path'] = file_path
    data['type'] = 'photo'

  # Если прислали видео
  elif message.video:
    file_path = os.path.join(MEDIA_DIR, f"{chat_id}_{msg_id}.mp4")
    await bot.download(message.video.file_id, destination=file_path)
    data['file_path'] = file_path
    data['type'] = 'video'

  # Сохраняем в кэш
  db[f"{chat_id}_{msg_id}"] = data


@dp.deleted_business_messages()
async def handle_deleted_business_messages(
    event: types.BusinessMessagesDeleted,
):
  """Отслеживает удаление сообщений собеседником."""
  chat_id = event.chat.id

  for msg_id in event.message_ids:
    key = f"{chat_id}_{msg_id}"

    if key in db:
      saved_item = db[key]
      header = (
          f"🗑 **УДАЛЕНО СООБЩЕНИЕ**\n\n"
          f"👤 **От кого:** {saved_item['sender_info']}\n"
          f"🆔 **ID чата:** `{chat_id}`\n"
      )

      try:
        # Отправляем текст или медиа в ваш личный чат с ботом
        if saved_item['type'] == 'photo' and os.path.exists(
            saved_item['file_path']
        ):
          photo_file = types.FSInputFile(saved_item['file_path'])
          await bot.send_photo(
              chat_id=OWNER_ID,
              photo=photo_file,
              caption=(
                  f"{header}📷 **Фото**\n📝 **Подпись:**"
                  f" {saved_item['text'] or 'Без подписи'}"
              ),
              parse_mode=ParseMode.MARKDOWN,
          )
          os.remove(saved_item['file_path'])  # Очищаем память

        elif saved_item['type'] == 'video' and os.path.exists(
            saved_item['file_path']
        ):
          video_file = types.FSInputFile(saved_item['file_path'])
          await bot.send_video(
              chat_id=OWNER_ID,
              video=video_file,
              caption=(
                  f"{header}🎥 **Видео**\n📝 **Подпись:**"
                  f" {saved_item['text'] or 'Без подписи'}"
              ),
              parse_mode=ParseMode.MARKDOWN,
          )
          os.remove(saved_item['file_path'])  # Очищаем память

        else:
          text_content = saved_item['text'] or "Сообщение без текста/медиа"
          await bot.send_message(
              chat_id=OWNER_ID,
              text=f"{header}💬 **Текст:** {text_content}",
              parse_mode=ParseMode.MARKDOWN,
          )

      except Exception as e:
        print(f"Ошибка при отправке лога удаления: {e}")

      # Удаляем запись из кэша
      del db[key]


async def main():
  print("🚀 Бот для авто-сохранения и анти-удаления запущен!")
  await dp.start_polling(bot)


if __name__ == "__main__":
  asyncio.run(main())
