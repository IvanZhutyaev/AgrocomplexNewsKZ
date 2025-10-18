import asyncio
import os
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import FSInputFile
from config import BOT_TOKEN, CHANNEL_ID, ADMINS
from database import init_db, add_site, remove_site, get_sites, is_news_sent, mark_news_sent, mark_news_published, \
    get_queue_size, clear_stuck_processing
from site_poster import post_news_to_site
from news_sender import send_processed_news_to_admin, get_pending_raw_news, get_pending_processed_news, \
    remove_from_pending_raw_news, remove_from_pending_processed_news, delete_news_messages

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


# Команды для админов
def is_admin(user_id: int) -> bool:
    return user_id in ADMINS


# Обработка одобрения сырой новости
@dp.callback_query(F.data.startswith("approve_raw|"))
async def approve_raw_news(callback: types.CallbackQuery):
    await callback.answer("✅ Новость одобрена для редактирования")

    _, news_id = callback.data.split("|", 1)
    data = get_pending_raw_news().get(news_id)
    if not data:
        await delete_news_messages(callback.from_user.id, news_id)
        await callback.message.answer("❌ Новость не найдена.")
        return

    # Удаляем все сообщения этой новости у админа
    await delete_news_messages(callback.from_user.id, news_id)

    # Обрабатываем через DeepSeek
    from parser import process_with_deepseek
    processed_text = await process_with_deepseek(data["title"], data["text"])

    # Отправляем обработанную новость на финальное одобрение
    await send_processed_news_to_admin(processed_text, data["url"], data["title"])

    # Удаляем из временного хранилища
    remove_from_pending_raw_news(news_id)

    # Уведомляем админа
    await callback.message.answer("✅ Новость отправлена на обработку DeepSeek")


# Обработка отклонения сырой новости
@dp.callback_query(F.data.startswith("reject_raw|"))
async def reject_raw_news(callback: types.CallbackQuery):
    try:
        await callback.answer("❌ Новость отклонена")
    except Exception:
        pass

    _, news_id = callback.data.split("|", 1)

    # Удаляем все сообщения этой новости у админа
    await delete_news_messages(callback.from_user.id, news_id)

    remove_from_pending_raw_news(news_id)

    # Уведомляем ВСЕХ админов об отклонении
    for admin_id in ADMINS:
        try:
            await bot.send_message(admin_id, "❌ Сырая новость отклонена.")
        except Exception:
            pass


# Подтверждение обработанной новости для Telegram
@dp.callback_query(F.data.startswith("approve|"))
async def approve_processed_news(callback: types.CallbackQuery):
    await callback.answer()

    _, news_id = callback.data.split("|", 1)
    data = get_pending_processed_news().get(news_id)
    if not data:
        await delete_news_messages(callback.from_user.id, news_id)
        await callback.message.answer("❌ Новость не найдена.")
        return

    news_text = data["text"]
    image_path = data["image"]

    if not os.path.exists(image_path):
        print(f"❌ Файл не найден: {image_path}")
        await delete_news_messages(callback.from_user.id, news_id)
        await callback.message.answer("❌ Изображение не найдено, новость не отправлена.")
        return

    photo = FSInputFile(image_path)

    try:
        # Сначала отправляем фото с началом текста (если текст короткий)
        if len(news_text) <= 1024:
            # Если текст помещается в подпись к фото
            await bot.send_photo(CHANNEL_ID, photo, caption=news_text, parse_mode="HTML")
        else:
            # Если текст длинный - отправляем фото без текста, а текст отдельно
            await bot.send_photo(CHANNEL_ID, photo)
            # Отправляем текст частями
            from news_sender import send_long_message
            await send_long_message(CHANNEL_ID, news_text, "")

    except Exception as e:
        print("❌ Ошибка отправки в канал:", e)
        await delete_news_messages(callback.from_user.id, news_id)
        await callback.message.answer("❌ Не удалось отправить новость в канал.")
        return

    # Отмечаем как опубликованную
    await mark_news_published(data["url"])
    remove_from_pending_processed_news(news_id)

    # Удаляем все сообщения этой новости у админа
    await delete_news_messages(callback.from_user.id, news_id)

    # Уведомляем ВСЕХ админов о публикации
    for admin_id in ADMINS:
        try:
            await bot.send_message(admin_id, "✅ Новость опубликована в Telegram.")
        except Exception:
            pass


@dp.callback_query(F.data.startswith("site|"))
async def post_to_site(callback: types.CallbackQuery):
    try:
        await callback.answer()
        _, news_id = callback.data.split("|", 1)
        data = get_pending_processed_news().get(news_id)
        if not data:
            await delete_news_messages(callback.from_user.id, news_id)
            await callback.message.answer("❌ Новость не найдена.")
            return

        success = post_news_to_site(data["text"], data["image"])
        if success:
            await mark_news_published(data["url"])
            remove_from_pending_processed_news(news_id)
            await delete_news_messages(callback.from_user.id, news_id)
            await callback.message.answer("🌐 Новость опубликована на сайте!")

            for admin_id in ADMINS:
                try:
                    await bot.send_message(admin_id, "🌐 Новость опубликована на сайте!")
                except Exception:
                    pass
        else:
            await delete_news_messages(callback.from_user.id, news_id)
            await callback.message.answer("❌ Ошибка при публикации на сайте.")
    except Exception as e:
        print(f"❌ Ошибка в post_to_site: {e}")
        await delete_news_messages(callback.from_user.id, news_id)
        await callback.message.answer("❌ Произошла ошибка при публикации на сайте.")


@dp.callback_query(F.data.startswith("both|"))
async def post_to_both(callback: types.CallbackQuery):
    try:
        await callback.answer()
        _, news_id = callback.data.split("|", 1)
        data = get_pending_processed_news().get(news_id)
        if not data:
            await delete_news_messages(callback.from_user.id, news_id)
            await callback.message.answer("❌ Новость не найдена.")
            return

        image_path = data["image"]
        text = data["text"]

        # 1️⃣ Публикуем на сайт
        success_site = post_news_to_site(text, image_path)

        # 2️⃣ Публикуем в Telegram
        try:
            photo = FSInputFile(image_path)
            if len(text) <= 1024:
                await bot.send_photo(CHANNEL_ID, photo, caption=text, parse_mode="HTML")
            else:
                await bot.send_photo(CHANNEL_ID, photo)
                from news_sender import send_long_message
                await send_long_message(CHANNEL_ID, text, "")
            success_tg = True
        except Exception as e:
            print("Ошибка публикации в Telegram:", e)
            success_tg = False

        # Результат
        if success_site or success_tg:
            await mark_news_published(data["url"])
            remove_from_pending_processed_news(news_id)
            await delete_news_messages(callback.from_user.id, news_id)

            result_message = ""
            if success_site and success_tg:
                result_message = "🚀 Новость опубликована в Telegram и на сайте!"
            elif success_site:
                result_message = "🌐 Новость опубликована на сайте (Telegram не удалось)!"
            else:
                result_message = "✅ Новость опубликована в Telegram (сайт не удалось)!"

            await callback.message.answer(result_message)
            for admin_id in ADMINS:
                try:
                    await bot.send_message(admin_id, result_message)
                except Exception:
                    pass
        else:
            await delete_news_messages(callback.from_user.id, news_id)
            await callback.message.answer("⚠️ Ошибка при публикации (проверь лог).")
    except Exception as e:
        print(f"❌ Ошибка в post_to_both: {e}")
        await delete_news_messages(callback.from_user.id, news_id)
        await callback.message.answer("❌ Произошла ошибка при публикации.")


@dp.callback_query(F.data.startswith("reject|"))
async def reject_processed_news(callback: types.CallbackQuery):
    try:
        await callback.answer("❌ Новость отклонена")
    except Exception:
        pass

    _, news_id = callback.data.split("|", 1)

    # Удаляем все сообщения этой новости у админа
    await delete_news_messages(callback.from_user.id, news_id)

    remove_from_pending_processed_news(news_id)

    for admin_id in ADMINS:
        try:
            await bot.send_message(admin_id, "❌ Обработанная новость отклонена.")
        except Exception:
            pass

async def delete_message_safe(callback: types.CallbackQuery):
    """Безопасно удаляет сообщение, обрабатывая возможные ошибки"""
    try:
        await callback.message.delete()
        print("✅ Сообщение с новостью удалено из чата")
    except Exception as e:
        print(f"⚠️ Не удалось удалить сообщение: {e}")
        # Пробуем отредактировать сообщение, если удалить не получилось
        try:
            await callback.message.edit_text(
                "🗑️ Новость обработана и удалена из чата",
                reply_markup=None
            )
        except Exception:
            pass

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer("❌ Ты не админ!")
        return

    help_text = """
🤖 *Панель управления Новостным Ботом*

*📋 Основные команды:*

*/start* - показать это сообщение
*/help* - подробная справка по командам

*🌐 Управление RSS-лентами:*
*/addsite <url>* - добавить RSS-ленту
*/listsites* - показать все RSS-ленты  
*/removesite <url>* - удалить RSS-ленту

*📊 Управление очередью и мониторинг:*
*/queue* - статус очереди новостей
*/postnext* - обработать следующую новость вручную
*/skipnext* - пропустить зависшую новость
*/postlatest* - принудительно проверить RSS-ленты
*/force_check* - массовая проверка всех RSS (до 15 новостей с каждого)

*🚀 Автоматический процесс модерации:*

*1 ЭТАП - Сырая новость:*
• 📥 Новости автоматически парсятся из RSS-лент
• ⏳ Добавляются в очередь обработки
• 👁️ Сырая новость приходит всем админам для первичного одобрения
• ✅ *Одобрить для редактирования* - отправляет на AI-обработку через DeepSeek
• ❌ *Отклонить* - удаляет новость из системы

*2 ЭТАП - Обработанная новость:*
• 🤖 DeepSeek переписывает новость в профессиональном стиле
• ✍️ Обработанная новость приходит на финальную модерацию
• 🌐 *На сайт* - опубликовать только на сайте agrosearch.kz
• ✅ *В Telegram* - опубликовать только в Telegram-канал  
• 🚀 *Оба* - опубликовать везде
• ❌ *Отклонить* - удалить новость

*⚙️ Технические особенности:*

• *Автопарсинг*: Каждые 30 секунд проверяет RSS-ленты
• *Очередь*: Новости обрабатываются по одной для избежания спама
• *Изображения*: Случайные картинки из папки /images
• *AI-обработка*: DeepSeek переписывает текст (250-300 слов)
• *Мультиязычность*: Публикация на сайте только на русском
• *Дубликаты*: Система автоматически избегает повторных публикаций

*📈 Статистика:*
• Отслеживание размера очереди
• Мониторинг новостей на модерации
• История опубликованных новостей

Для начала работы добавьте RSS-ленты командой /addsite
Примеры популярных RSS:
• `https://www.agroinvestor.ru/news/rss/`
• `https://www.agroxxi.ru/export/rss.xml`
• `https://ria.ru/export/rss2/agriculture/index.xml`
    """

    await message.answer(help_text, parse_mode="Markdown")


@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer("❌ Ты не админ!")
        return

    help_text = """
📖 *Подробная справка по командам бота*

*🌐 ДОБАВЛЕНИЕ И УПРАВЛЕНИЕ RSS-ЛЕНТАМИ:*

`/addsite https://example.com/rss` - добавить одну RSS-ленту
`/addsite https://site1.com/rss, https://site2.com/rss` - добавить несколько через запятую
`/listsites` - посмотреть все активные RSS-ленты
`/removesite https://example.com/rss` - удалить RSS-ленту

*📊 УПРАВЛЕНИЕ ОЧЕРЕДЬЮ И МОНИТОРИНГ:*

`/queue` - посмотреть текущий статус:
   • Новостей в очереди
   • Сырых на модерации  
   • Обработанных на финальной проверке
   • Всего админов в системе

`/postnext` - вручную запустить обработку следующей новости из очереди
`/skipnext` - пропустить зависшую новость (очищает блокировки)
`/postlatest` - принудительно проверить ВСЕ RSS-ленты (по 1 новости с каждого)
`/force_check` - массовая проверка (до 15 новостей с каждой ленты)

*🔄 АВТОМАТИЧЕСКИЙ ПРОЦЕСС:*

1. *Парсинг*: Каждые 30 сек проверяются ВСЕ RSS-ленты
2. *Фильтрация*: Исключаются уже опубликованные новости
3. *Очередь*: Новые новости добавляются в очередь обработки
4. *Первая модерация*: Сырая новость приходит всем админам
5. *AI-обработка*: После одобрения → DeepSeek переписывает текст
6. *Финальная модерация*: Обработанная новость на утверждение
7. *Публикация*: Выбор платформ (сайт, Telegram, оба)

*🎯 ВОЗМОЖНОСТИ СИСТЕМЫ:*

• *Умный парсинг*: Автоопределение основного контента статьи
• *AI-рерайт*: Профессиональное переписывание через DeepSeek
• *Автоизображения*: Случайный выбор картинок из базы
• *Защита от дублей*: Контроль по URL-ссылкам
• *Мульти-админ*: Все админы получают уведомления
• *Логирование*: Детальная статистика обработки

*⚠️ УСТРАНЕНИЕ ПРОБЛЕМ:*

• *Нет новостей*: Используйте `/force_check` для принудительной проверки
• *Зависла очередь*: Используйте `/skipnext` для очистки блокировок
• *Ошибки публикации*: Проверьте логи в консоли бота

*🔧 РЕКОМЕНДАЦИИ:*

• Добавьте 3-5 RSS-лент для стабильного потока новостей
• Регулярно проверяйте `/queue` для мониторинга
• Используйте `/force_check` раз в день для полного обновления

Для старта добавьте хотя бы одну RSS-ленту командой `/addsite`!
    """

    await message.answer(help_text, parse_mode="Markdown")


@dp.message(Command("addsite"))
async def cmd_add_site(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer("❌ Ты не админ!")
        return

    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer(
            "❌ Укажи ссылки на RSS (можно несколько через запятую или с новой строки)\n\n"
            "Примеры:\n"
            "• `/addsite https://example1.com/rss, https://example2.com/rss`\n"
            "• `/addsite https://example1.com/rss`\n"
            "    `https://example2.com/rss`\n"
            "    `https://example3.com/rss`",
            parse_mode="Markdown"
        )
        return

    urls_text = args[1].strip()

    # Разбиваем ссылки по разным разделителям
    urls = []
    for line in urls_text.split('\n'):
        line = line.strip()
        if not line:
            continue
        # Разбиваем по запятым в одной строке
        for url in line.split(','):
            url = url.strip()
            if url:
                urls.append(url)

    if not urls:
        await message.answer("❌ Не найдено валидных ссылок")
        return

    added_urls = []
    error_urls = []

    for url in urls:
        # Простая валидация URL
        if not url.startswith(('http://', 'https://')):
            error_urls.append(f"{url} (неверный формат)")
            continue

        try:
            await add_site(url)
            added_urls.append(url)
        except Exception as e:
            error_urls.append(f"{url} ({str(e)})")

    # Формируем ответ
    result_message = ""

    if added_urls:
        result_message += f"✅ Добавлено {len(added_urls)} RSS-лент:\n"
        for url in added_urls:
            result_message += f"• `{url}`\n"

    if error_urls:
        if result_message:
            result_message += "\n"
        result_message += f"❌ Ошибки ({len(error_urls)}):\n"
        for error in error_urls:
            result_message += f"• {error}\n"

    await message.answer(result_message, parse_mode="Markdown")


@dp.message(Command("listsites"))
async def cmd_list_sites(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer("❌ Ты не админ!")
        return

    sites = await get_sites()
    if not sites:
        await message.answer("📭 RSS-ленты не добавлены\nИспользуй /addsite для добавления")
        return

    sites_text = "📋 *Добавленные RSS-ленты:*\n\n"
    for i, site in enumerate(sites, 1):
        sites_text += f"{i}. `{site}`\n"

    sites_text += f"\nВсего: {len(sites)} лент"
    await message.answer(sites_text, parse_mode="Markdown")


@dp.message(Command("removesite"))
async def cmd_remove_site(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer("❌ Ты не админ!")
        return

    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer(
            "❌ Укажи ссылки на RSS для удаления (можно несколько через запятую или с новой строки)\n\n"
            "Примеры:\n"
            "• `/removesite https://example1.com/rss, https://example2.com/rss`\n"
            "• `/removesite https://example1.com/rss`\n"
            "    `https://example2.com/rss`\n"
            "    `https://example3.com/rss`\n\n"
            "📋 Сначала посмотри список сайтов: /listsites",
            parse_mode="Markdown"
        )
        return

    urls_text = args[1].strip()

    # Разбиваем ссылки по разным разделителям
    urls = []
    for line in urls_text.split('\n'):
        line = line.strip()
        if not line:
            continue
        # Разбиваем по запятым в одной строке
        for url in line.split(','):
            url = url.strip()
            if url:
                urls.append(url)

    if not urls:
        await message.answer("❌ Не найдено валидных ссылок")
        return

    removed_urls = []
    error_urls = []
    not_found_urls = []

    # Получаем текущий список сайтов для проверки
    current_sites = await get_sites()

    for url in urls:
        # Проверяем, существует ли сайт в списке
        if url not in current_sites:
            not_found_urls.append(url)
            continue

        try:
            await remove_site(url)
            removed_urls.append(url)
        except Exception as e:
            error_urls.append(f"{url} ({str(e)})")

    # Формируем ответ
    result_message = ""

    if removed_urls:
        result_message += f"✅ Удалено {len(removed_urls)} RSS-лент:\n"
        for url in removed_urls:
            result_message += f"• `{url}`\n"

    if not_found_urls:
        if result_message:
            result_message += "\n"
        result_message += f"⚠️ Не найдено в списке ({len(not_found_urls)}):\n"
        for url in not_found_urls:
            result_message += f"• `{url}`\n"

    if error_urls:
        if result_message:
            result_message += "\n"
        result_message += f"❌ Ошибки удаления ({len(error_urls)}):\n"
        for error in error_urls:
            result_message += f"• {error}\n"

    if not removed_urls and not not_found_urls and not error_urls:
        result_message = "❌ Не удалось удалить ни одной RSS-ленты"

    await message.answer(result_message, parse_mode="Markdown")


@dp.message(Command("queue"))
async def cmd_queue_status(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer("❌ Ты не админ!")
        return

    queue_size = await get_queue_size()
    from news_sender import get_pending_raw_news, get_pending_processed_news
    pending_raw_count = len(get_pending_raw_news())
    pending_processed_count = len(get_pending_processed_news())

    status_text = (
        f"📊 *Статус системы*\n\n"
        f"• 📥 Новостей в очереди: *{queue_size}*\n"
        f"• ⏳ Сырых новостей на модерации: *{pending_raw_count}*\n"
        f"• ✍️ Обработанных новостей на модерации: *{pending_processed_count}*\n"
        f"• 👥 Всего админов: *{len(ADMINS)}*\n"
        f"\n*Процесс модерации:*\n"
        f"1. Сырая новость → Одобрение → DeepSeek\n"
        f"2. Обработанная новость → Публикация\n"
        f"\n*Управление очередью:*\n"
        f"`/postnext` - обработать следующую новость\n"
        f"`/skipnext` - пропустить текущую новость\n"
        f"`/postlatest` - принудительно проверить RSS\n"
        f"\n*Добавление лент:*\n"
        f"`/addsite <url>` - добавить RSS\n"
        f"`/listsites` - посмотреть ленты"
    )

    await message.answer(status_text, parse_mode="Markdown")


@dp.message(Command("postnext"))
async def cmd_post_next(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer("❌ Ты не админ!")
        return

    # Запускаем обработку следующей новости из очереди
    from parser import process_next_from_queue
    success = await process_next_from_queue()

    if success:
        await message.answer("✅ Следующая новость отправлена на первичную модерацию всем админам!")
    else:
        await message.answer("❌ В очереди нет новых новостей.")


@dp.message(Command("skipnext"))
async def cmd_skip_next(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer("❌ Ты не админ!")
        return

    # Пропускаем текущую новость (очищаем зависшие обработки)
    await clear_stuck_processing()
    await message.answer("✅ Зависшие обработки очищены. Следующая новость будет обработана автоматически.")


@dp.message(Command("postlatest"))
async def cmd_post_latest(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer("❌ Ты не админ!")
        return

    sites = await get_sites()
    if not sites:
        await message.answer("❌ Сайты не добавлены! Используй /addsite")
        return

    await message.answer("🔄 Принудительно проверяю RSS-ленты...")

    posted = 0
    for url in sites:
        from parser import parse_feed_and_process
        try:
            news_count = await parse_feed_and_process(url, limit=1)
            posted += news_count
            if news_count > 0:
                await message.answer(f"✅ Добавлено {news_count} новостей в очередь из:\n`{url}`", parse_mode="Markdown")
        except Exception as e:
            await message.answer(f"❌ Ошибка при проверке {url}:\n`{e}`", parse_mode="Markdown")

    if posted == 0:
        await message.answer("ℹ️ Новых новостей для публикации нет.")
    else:
        await message.answer(f"🎯 Всего добавлено в очередь: {posted} новостей")


# Добавьте эту команду в bot.py для ручного запуска
@dp.message(Command("force_check"))
async def cmd_force_check(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    await message.answer("🔄 Принудительная проверка всех RSS...")
    sites = await get_sites()
    total_added = 0

    for url in sites:
        from parser import parse_feed_and_process
        added = await parse_feed_and_process(url, limit=15)  # Больше новостей
        total_added += added
        await asyncio.sleep(1)

    await message.answer(f"✅ Добавлено {total_added} новостей в очередь")

# Обработчик для любых других сообщений
@dp.message()
async def handle_other_messages(message: types.Message):
    if is_admin(message.from_user.id):
        await message.answer(
            "ℹ️ Используй /help для просмотра всех команд\n"
            "или /start для начала работы"
        )
    else:
        await message.answer("❌ У тебя нет доступа к этому боту.")


# Инициализация базы данных при запуске
async def initialize():
    await init_db()
    print("✅ База данных инициализирована")


# Запуск инициализации
async def on_startup():
    await initialize()