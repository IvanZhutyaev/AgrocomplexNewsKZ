import asyncio

import site_poster
from bot import dp, bot
from parser import scheduler
import logging
import signal
import sys

async def main():
    print("🤖 Бот запускается...")

    # Настройка логирования
    logging.basicConfig(level=logging.INFO)
    site_poster.find_correct_form_endpoint()
    # Запускаем фоновую задачу парсера
    parser_task = asyncio.create_task(scheduler())

    # Запускаем бота
    try:
        await dp.start_polling(bot, handle_signals=False)
    except Exception as e:
        print(f"❌ Ошибка бота: {e}")
    finally:
        # Корректно останавливаем задачи
        parser_task.cancel()
        try:
            await parser_task
        except asyncio.CancelledError:
            pass

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("👋 Бот остановлен")
    except Exception as e:
        print(f"❌ Критическая ошибка: {e}")