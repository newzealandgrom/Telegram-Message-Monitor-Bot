import os
from telethon import TelegramClient, events, types, Button
from telethon.tl import functions, types
from telethon.network import ConnectionTcpFull
import asyncio
import logging
import json
import time
import re

# Настройка логирования
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Конфигурация
API_ID = "ВАШ API_ID"
API_HASH = "ВАШ API_HASH"
BOT_TOKEN = "ВАШ BOT_TOKEN"

# Файл для хранения настроек
SETTINGS_FILE = "monitor_settings.json"


class MessageMonitor:
    def __init__(self):
        self.client = None
        self.bot = None
        self.monitored_groups = {}
        self.owner_id = None
        self.search_query = {}  # Для хранения поисковых запросов пользователей
        self.page_size = 5  # Количество групп на одной странице
        self.processed_messages = set()  # Для отслеживания обработанных сообщений
        self.entity_cache = (
            {}
        )  # Кэш для entity, чтобы уменьшить количество API-запросов
        self.load_settings()

    def load_settings(self):
        if os.path.exists(SETTINGS_FILE):
            try:
                with open(SETTINGS_FILE, "r") as f:
                    self.monitored_groups = json.load(f)
                logger.info(
                    f"Загружены настройки: отслеживается {len(self.monitored_groups)} групп"
                )
            except Exception as e:
                logger.error(f"Ошибка при загрузке настроек: {e}")
                self.monitored_groups = {}

    def save_settings(self):
        try:
            with open(SETTINGS_FILE, "w") as f:
                json.dump(self.monitored_groups, f)
            # Создаем копию файла настроек для защиты от потери данных
            with open(f"{SETTINGS_FILE}.backup", "w") as f:
                json.dump(self.monitored_groups, f)
            logger.info(
                f"Настройки сохранены: отслеживается {len(self.monitored_groups)} групп"
            )
        except Exception as e:
            logger.error(f"Ошибка при сохранении настроек: {e}")

    def normalize_group_id(self, group_id):
        """Нормализует ID группы для совместимости с разными форматами"""
        # Если ID уже строка, работаем с ней, иначе преобразуем
        str_id = str(group_id)

        # Формат: -100... (полный формат)
        if str_id.startswith("-100"):
            return str_id

        # Формат: -... (короткий формат с минусом)
        if str_id.startswith("-"):
            # Добавляем префикс -100 если его нет
            return f"-100{str_id[1:]}"

        # Формат: число без минуса
        return f"-100{str_id}"

    def is_group_monitored(self, group_id):
        """Проверяет, отслеживается ли группа (поддерживает разные форматы ID)"""
        str_id = str(group_id)

        # Пробуем все возможные форматы ID
        formats = [
            str_id,  # Оригинальный формат
            str_id.replace("-100", "-"),  # Короткий формат (без 100)
            f"-100{str_id[1:]}"
            if str_id.startswith("-")
            else f"-100{str_id}",  # Полный формат
        ]

        # Проверяем все варианты
        for id_format in formats:
            if id_format in self.monitored_groups:
                return True, id_format

        return False, None

    async def get_entity_safely(self, chat_id, max_retries=3):
        """Безопасно получает entity с повторными попытками и кэшированием"""
        cache_key = str(chat_id)

        # Проверяем кэш сначала
        if cache_key in self.entity_cache:
            entity = self.entity_cache[cache_key]
            if (
                time.time() - entity["timestamp"] < 3600
            ):  # Кэш действителен в течение часа
                logger.info(f"Использование кэшированного entity для {chat_id}")
                return entity["data"]

        # Пробуем разные форматы ID
        formats_to_try = [
            chat_id,  # Оригинальный формат
            int(chat_id)
            if chat_id.lstrip("-").isdigit()
            else chat_id,  # Как число если возможно
            int(chat_id.replace("-100", "-"))
            if chat_id.startswith("-100") and chat_id[4:].isdigit()
            else chat_id,  # Короткий формат
        ]

        # Пробуем получить entity с повторами
        last_error = None
        for attempt in range(max_retries):
            for id_format in formats_to_try:
                try:
                    entity = await self.client.get_entity(id_format)
                    # Сохраняем в кэш
                    self.entity_cache[cache_key] = {
                        "data": entity,
                        "timestamp": time.time(),
                    }
                    logger.info(
                        f"Успешно получен entity для {chat_id} (формат: {id_format})"
                    )
                    return entity
                except Exception as e:
                    last_error = e
                    logger.warning(f"Не удалось получить entity для {id_format}: {e}")

            # Если все форматы не сработали, ждем перед повторной попыткой
            if attempt < max_retries - 1:
                retry_delay = (attempt + 1) * 2  # Экспоненциальная задержка
                logger.info(
                    f"Ожидание {retry_delay} секунд перед повторной попыткой получения entity"
                )
                await asyncio.sleep(retry_delay)

        # Если все попытки не удались
        logger.error(
            f"Не удалось получить entity для {chat_id} после {max_retries} попыток: {last_error}"
        )
        return None

    async def delayed_reaction(self, event, chat_id):
        """Установка отложенной реакции на сообщение"""
        try:
            message_id = event.message.id
            message_key = f"{chat_id}_{message_id}"

            # Проверяем, не обрабатывали ли мы уже это сообщение
            if message_key in self.processed_messages:
                logger.info(f"Сообщение {message_key} уже обработано")
                return

            # Добавляем сообщение в список обработанных
            self.processed_messages.add(message_key)

            # Ждем 2 минуты
            logger.info(
                f"Запланирована реакция на сообщение в чате {chat_id}, id {message_id} через 2 минуты"
            )
            await asyncio.sleep(120)

            logger.info(
                f"Прошло 2 минуты, добавляем реакцию к сообщению {message_id} в чате {chat_id}"
            )

            # Получаем entity заранее
            entity = await self.get_entity_safely(chat_id)
            if not entity:
                logger.error(f"❌ Не удалось найти чат {chat_id} для добавления реакции")
                # Удаляем из мониторинга, если чат недоступен
                if chat_id in self.monitored_groups:
                    await self.stop_monitoring(chat_id)
                    logger.info(
                        f"Чат {chat_id} удален из мониторинга, так как недоступен"
                    )
                    # Уведомляем владельца
                    try:
                        await self.bot.send_message(
                            self.owner_id,
                            f"⚠️ Чат с ID {chat_id} был удален из мониторинга, так как бот больше не имеет к нему доступа.",
                        )
                    except Exception as notify_error:
                        logger.error(f"Не удалось уведомить владельца: {notify_error}")

                self.processed_messages.remove(message_key)
                return

            # Добавляем реакцию, перебирая несколько методов
            success = False

            # Метод 1: Прямой вызов API с использованием полученного entity
            try:
                await self.client(
                    functions.messages.SendReactionRequest(
                        peer=entity,
                        msg_id=message_id,
                        reaction=[types.ReactionEmoji(emoticon="👀")],
                    )
                )
                logger.info(
                    f"✅ Реакция успешно добавлена методом 1 к сообщению {message_id}"
                )
                success = True
            except Exception as e1:
                logger.error(f"❌ Ошибка метода 1: {str(e1)}")

                # Метод 2: Через react с сообщением
                if not success:
                    try:
                        # Получаем сообщение снова для актуального состояния
                        message = await self.client.get_messages(entity, ids=message_id)
                        if message:
                            await message.react("👀")
                            logger.info(f"✅ Реакция успешно добавлена методом 2")
                            success = True
                        else:
                            logger.warning(f"Сообщение {message_id} больше не доступно")
                    except Exception as e2:
                        logger.error(f"❌ Ошибка метода 2: {str(e2)}")

                        # Метод 3: Попробуем через одиночную реакцию
                        if not success:
                            try:
                                await self.client(
                                    functions.messages.SendReactionRequest(
                                        peer=entity,
                                        msg_id=message_id,
                                        reaction=types.ReactionEmoji(emoticon="👀"),
                                    )
                                )
                                logger.info(f"✅ Реакция успешно добавлена методом 3")
                                success = True
                            except Exception as e3:
                                logger.error(f"❌ Ошибка метода 3: {str(e3)}")

                                # Метод 4: Попробуем через строку
                                if not success:
                                    try:
                                        await self.client(
                                            functions.messages.SendReactionRequest(
                                                peer=entity,
                                                msg_id=message_id,
                                                reaction="👀",
                                            )
                                        )
                                        logger.info(
                                            f"✅ Реакция успешно добавлена методом 4"
                                        )
                                        success = True
                                    except Exception as e4:
                                        logger.error(f"❌ Ошибка метода 4: {str(e4)}")

            if not success:
                logger.error(
                    f"❌ Не удалось добавить реакцию ни одним из методов к сообщению {message_id}"
                )

            # Очищаем запись об обработке, если не удалось
            if not success:
                self.processed_messages.remove(message_key)

            # Очищаем старые записи, если в списке более 1000 элементов
            if len(self.processed_messages) > 1000:
                self.processed_messages = set(list(self.processed_messages)[-1000:])

        except Exception as e:
            logger.error(f"Общая ошибка в delayed_reaction: {str(e)}")

    async def validate_monitored_groups(self):
        """Проверяет доступность всех отслеживаемых групп"""
        groups_to_remove = []

        for group_id in self.monitored_groups:
            try:
                entity = await self.get_entity_safely(group_id)
                if not entity:
                    logger.warning(
                        f"Группа {group_id} недоступна и будет удалена из мониторинга"
                    )
                    groups_to_remove.append(group_id)
            except Exception as e:
                logger.error(f"Ошибка при проверке группы {group_id}: {e}")
                groups_to_remove.append(group_id)

        # Удаляем недоступные группы
        for group_id in groups_to_remove:
            await self.stop_monitoring(group_id)

        if groups_to_remove:
            logger.info(
                f"Удалено {len(groups_to_remove)} недоступных групп из мониторинга"
            )

            # Уведомляем владельца, если есть удаленные группы
            try:
                if self.owner_id and groups_to_remove:
                    message = f"⚠️ {len(groups_to_remove)} групп были удалены из мониторинга, так как недоступны:\n"
                    for group_id in groups_to_remove[:10]:  # Показываем первые 10
                        message += f"- {group_id}\n"
                    if len(groups_to_remove) > 10:
                        message += f"...и еще {len(groups_to_remove) - 10} групп"

                    await self.bot.send_message(self.owner_id, message)
            except Exception as notify_error:
                logger.error(
                    f"Не удалось уведомить владельца о недоступных группах: {notify_error}"
                )

    async def get_unique_groups(self):
        """Получает уникальный список групп без дубликатов"""
        try:
            dialogs = []
            seen_ids = set()

            async for dialog in self.client.iter_dialogs():
                if isinstance(dialog.entity, (types.Channel, types.Chat)):
                    # Проверка на дубликаты
                    if dialog.entity.id not in seen_ids:
                        seen_ids.add(dialog.entity.id)
                        dialogs.append(dialog.entity)

            return dialogs

        except Exception as e:
            logger.error(f"Ошибка при получении списка групп: {e}")
            return []

    async def get_groups(self, search_query=None):
        """Получает список групп с опциональным поиском"""
        groups = await self.get_unique_groups()

        # Если есть поисковый запрос, фильтруем группы
        if search_query:
            search_query = search_query.lower()
            filtered_groups = []
            for group in groups:
                if hasattr(group, "title") and search_query in group.title.lower():
                    filtered_groups.append(group)
            return filtered_groups

        return groups

    async def get_paginated_groups(self, page=0, search_query=None):
        """Возвращает группы с пагинацией"""
        groups = await self.get_groups(search_query)

        # Общее количество страниц
        total_pages = (len(groups) - 1) // self.page_size + 1 if groups else 1

        # Корректируем номер страницы, если он выходит за пределы
        page = max(0, min(page, total_pages - 1)) if total_pages > 0 else 0

        # Получаем группы для текущей страницы
        start_idx = page * self.page_size
        end_idx = min(start_idx + self.page_size, len(groups))
        page_groups = groups[start_idx:end_idx] if groups else []

        return page_groups, page, total_pages

    async def start(self):
        try:
            # Инициализация клиента для основного аккаунта
            self.client = TelegramClient(
                "user_session",
                API_ID,
                API_HASH,
                device_model="Desktop",
                system_version="Windows 10",
                app_version="1.0",
                lang_code="ru",
                system_lang_code="ru",
                connection_retries=10,  # Увеличено количество повторных попыток
                retry_delay=2,  # Увеличена задержка между попытками
                timeout=60,  # Увеличен таймаут
                connection=ConnectionTcpFull,
                sequential_updates=True,
                auto_reconnect=True,
            )

            if os.path.exists("user_session.session"):
                logger.info("Найден существующий файл сессии")
            else:
                logger.warning("Файл сессии не найден, потребуется авторизация")

            await self.client.start()
            logger.info("Основной клиент запущен")

            if await self.client.is_user_authorized():
                me = await self.client.get_me()
                self.owner_id = me.id  # Сохраняем ID владельца
                logger.info(f"Успешная авторизация как {me.first_name} (ID: {me.id})")
            else:
                logger.error("Клиент не авторизован!")
                return

        except Exception as e:
            logger.error(f"Ошибка при запуске основного клиента: {str(e)}")
            return

        try:
            # Инициализация бота
            self.bot = TelegramClient(
                "bot_session",
                API_ID,
                API_HASH,
                device_model="Desktop",
                system_version="Windows 10",
                app_version="1.0",
                lang_code="ru",
                system_lang_code="ru",
                connection_retries=10,  # Увеличено количество повторных попыток
                retry_delay=2,  # Увеличена задержка между попытками
                timeout=60,  # Увеличен таймаут
                connection=ConnectionTcpFull,
                sequential_updates=True,
                auto_reconnect=True,
            )

            await self.bot.start(bot_token=BOT_TOKEN)
            logger.info("Бот успешно запущен")

            # Настройка обработчиков
            await self.setup_handlers()

            # Проверяем доступность групп в мониторинге
            await self.validate_monitored_groups()

        except Exception as e:
            logger.error(f"Ошибка при запуске бота: {str(e)}")
            return

    async def setup_handlers(self):
        """Настраивает все обработчики событий"""

        # Обработчик для проверки доступа
        @self.bot.on(events.NewMessage())
        async def check_access(event):
            if event.sender_id != self.owner_id:
                await event.respond(
                    "⛔️ Этот бот является приватным и предназначен только для владельца."
                )
                logger.warning(
                    f"Попытка доступа от неавторизованного пользователя {event.sender_id}"
                )
                return

            if hasattr(event, "message") and hasattr(event.message, "text"):
                text = event.message.text
                if text == "/start":
                    await self.send_main_menu(event)
                elif text == "📋 Список групп":
                    # Сбрасываем поисковый запрос и показываем список групп
                    self.search_query[event.chat_id] = None
                    await self.show_groups_list(event, page=0)
                elif text == "🔍 Поиск групп":
                    await event.respond(
                        "Введите текст для поиска группы:", buttons=Button.force_reply()
                    )
                elif text == "🔍 Активные мониторинги":
                    await self.show_active_monitors(event)
                elif text == "❓ Помощь":
                    await self.show_help(event)
                elif text == "🔄 Проверить доступность":
                    await event.respond("⏳ Проверяю доступность отслеживаемых групп...")
                    await self.validate_monitored_groups()
                    await event.respond(
                        "✅ Проверка завершена! Недоступные группы удалены из мониторинга."
                    )
                elif text.startswith("🔍 Поиск: "):
                    # Извлекаем поисковый запрос и показываем результаты
                    search_text = text[len("🔍 Поиск: ") :]
                    self.search_query[event.chat_id] = search_text
                    await self.show_groups_list(event, page=0, search=search_text)
                elif event.message.is_reply and hasattr(
                    event.message.reply_to, "reply_to_msg_id"
                ):
                    # Проверяем, что это ответ на наше сообщение о поиске
                    try:
                        orig_msg = await event.get_reply_message()
                        if (
                            orig_msg
                            and orig_msg.text == "Введите текст для поиска группы:"
                        ):
                            search_text = event.message.text
                            self.search_query[event.chat_id] = search_text
                            await self.show_groups_list(
                                event, page=0, search=search_text
                            )
                    except Exception as e:
                        logger.error(f"Ошибка при обработке ответа: {e}")

        # Обработчик для callback запросов (кнопок)
        @self.bot.on(events.CallbackQuery())
        async def handle_callback(event):
            if event.sender_id != self.owner_id:
                await event.answer("⛔️ У вас нет доступа к этому боту.", alert=True)
                return

            try:
                data = event.data.decode()

                if data == "main_menu":
                    await self.send_main_menu(event)
                elif data.startswith("page_"):
                    # Навигация по страницам списка групп
                    page = int(data.split("_")[1])
                    search = self.search_query.get(event.chat_id)
                    await self.show_groups_list(
                        event, page=page, search=search, edit=True
                    )
                elif data.startswith("toggle_"):
                    # Включение/отключение мониторинга группы
                    await self.toggle_group_monitoring(event, data)
                elif data == "search_groups":
                    # Кнопка поиска групп
                    await event.answer(
                        "Используйте кнопку 'Поиск групп' в главном меню"
                    )
                elif data.startswith("mon_page_"):
                    # Навигация по страницам мониторингов
                    page = int(data.split("_")[2])
                    await self.show_active_monitors(event, page=page, edit=True)
                elif data == "check_availability":
                    # Проверка доступности отслеживаемых групп
                    await event.answer("Начинаю проверку доступности групп...")
                    await self.validate_monitored_groups()
                    try:
                        await event.edit(
                            "✅ Проверка завершена! Недоступные группы удалены из мониторинга."
                        )
                    except:
                        await event.respond(
                            "✅ Проверка завершена! Недоступные группы удалены из мониторинга."
                        )
                elif data == "dummy":
                    # Пустая кнопка, ничего не делаем
                    await event.answer("Информационная кнопка")
                else:
                    await event.answer("Неизвестная команда")
            except Exception as e:
                logger.error(f"Ошибка в обработчике callback: {e}")
                try:
                    await event.answer(
                        "Произошла ошибка при обработке команды", alert=True
                    )
                except:
                    pass

        # Обработчик для мониторинга новых сообщений в группах
        @self.client.on(events.NewMessage())
        async def monitor_messages(event):
            try:
                chat_id = str(event.chat_id)
                logger.info(f"Получено сообщение в чате {chat_id}")

                # Проверяем, отслеживается ли данный чат
                is_monitored, monitor_id = self.is_group_monitored(chat_id)

                if is_monitored:
                    logger.info(
                        f"Чат {chat_id} находится в списке отслеживаемых (ID: {monitor_id})"
                    )

                    try:
                        # Получаем информацию о себе
                        me = await self.client.get_me()
                        logger.info(
                            f"ID отправителя: {event.sender_id}, наш ID: {me.id}"
                        )

                        # Проверяем, что сообщение не от нас
                        if event.sender_id != me.id:
                            # Безопасно получаем entity
                            entity = await self.get_entity_safely(chat_id)

                            if not entity:
                                logger.error(
                                    f"Не удалось получить entity для чата {chat_id}"
                                )
                                return

                            # Отмечаем сообщение как прочитанное
                            try:
                                await self.client.send_read_acknowledge(
                                    entity, event.message, clear_mentions=True
                                )
                                logger.info(
                                    f"✅ Сообщение {event.message.id} отмечено как прочитанное"
                                )
                            except Exception as read_error:
                                logger.error(
                                    f"❌ Ошибка при отметке сообщения {event.message.id} как прочитанного: {str(read_error)}"
                                )

                            # Ставим сообщение в очередь на добавление реакции
                            logger.info(
                                f"Сообщение от другого пользователя, ставим в очередь на добавление реакции"
                            )

                            # Создаем задачу на отложенную реакцию
                            asyncio.create_task(self.delayed_reaction(event, chat_id))
                        else:
                            logger.info(
                                f"Пропускаем собственное сообщение {event.message.id}"
                            )
                    except Exception as e:
                        logger.error(
                            f"Ошибка при обработке сообщения {event.message.id}: {str(e)}"
                        )
                else:
                    logger.info(f"Чат {chat_id} не отслеживается")
            except Exception as e:
                logger.error(f"Ошибка при мониторинге сообщений: {str(e)}")

    async def send_main_menu(self, event):
        """Отправляет главное меню бота"""
        keyboard = [
            [Button.text("📋 Список групп")],
            [Button.text("🔍 Поиск групп")],
            [Button.text("🔍 Активные мониторинги")],
            [Button.text("🔄 Проверить доступность")],
            [Button.text("❓ Помощь")],
        ]

        try:
            if hasattr(event, "edit") and callable(event.edit):
                try:
                    await event.edit("Выберите действие:", buttons=keyboard)
                except Exception:
                    await event.respond("Выберите действие:", buttons=keyboard)
            else:
                await event.respond("Выберите действие:", buttons=keyboard)
        except Exception as e:
            logger.error(f"Ошибка при отправке меню: {e}")
            try:
                await event.respond("Выберите действие:", buttons=keyboard)
            except:
                pass

    async def show_groups_list(self, event, page=0, search=None, edit=False):
        """Показывает страницу со списком групп"""
        try:
            groups, current_page, total_pages = await self.get_paginated_groups(
                page, search
            )

            if not groups:
                message = "Не найдено ни одной группы/канала."
                if search:
                    message = f"По запросу '{search}' не найдено групп."

                if edit and hasattr(event, "edit") and callable(event.edit):
                    try:
                        await event.edit(message)
                    except:
                        await event.respond(message)
                else:
                    await event.respond(message)
                return

            # Создаем кнопки групп
            buttons = []
            for group in groups:
                if hasattr(group, "id") and hasattr(group, "title"):
                    is_monitored, _ = self.is_group_monitored(group.id)
                    status = "✅" if is_monitored else "⭕️"
                    button_text = f"{status} {group.title}"
                    buttons.append([Button.inline(button_text, f"toggle_{group.id}")])

            # Добавляем навигационные кнопки
            nav_buttons = []

            # Кнопка "Назад"
            if current_page > 0:
                nav_buttons.append(Button.inline("◀️ Назад", f"page_{current_page-1}"))

            # Информация о текущей странице
            page_info = f"📄 {current_page+1}/{total_pages}"
            nav_buttons.append(Button.inline(page_info, "dummy"))

            # Кнопка "Вперед"
            if current_page < total_pages - 1:
                nav_buttons.append(Button.inline("Вперед ▶️", f"page_{current_page+1}"))

            buttons.append(nav_buttons)

            # Добавляем поиск и главное меню
            search_info = f"🔍 Поиск: {search}" if search else "🔍 Поиск групп"
            buttons.append([Button.inline(search_info, "search_groups")])
            buttons.append([Button.inline("🔙 Главное меню", "main_menu")])

            message = "Выберите группу для включения/отключения мониторинга:"
            if search:
                message = f"Результаты поиска по запросу '{search}':"

            if edit and hasattr(event, "edit") and callable(event.edit):
                try:
                    await event.edit(message, buttons=buttons)
                except:
                    await event.respond(message, buttons=buttons)
            else:
                await event.respond(message, buttons=buttons)

        except Exception as e:
            logger.error(f"Ошибка при получении списка групп: {e}")
            message = "Произошла ошибка при получении списка групп. Попробуйте позже."
            await event.respond(message)

    async def show_active_monitors(self, event, page=0, edit=False):
        """Показывает активные мониторинги с пагинацией"""
        if not self.monitored_groups:
            message = "Нет активных мониторингов."
            if edit and hasattr(event, "edit") and callable(event.edit):
                try:
                    await event.edit(message)
                except:
                    await event.respond(message)
            else:
                await event.respond(message)
            return

        try:
            # Получаем список активных групп
            active_groups = []
            for group_id in self.monitored_groups:
                try:
                    # Получаем entity
                    entity = await self.get_entity_safely(group_id)

                    if entity and hasattr(entity, "title"):
                        active_groups.append(
                            {
                                "id": group_id,
                                "title": entity.title,
                                "status": "✅ Доступна",
                            }
                        )
                    else:
                        active_groups.append(
                            {
                                "id": group_id,
                                "title": f"Группа {group_id}",
                                "status": "❌ Недоступна",
                            }
                        )
                except Exception as e:
                    logger.error(
                        f"Ошибка при получении информации о группе {group_id}: {e}"
                    )
                    active_groups.append(
                        {
                            "id": group_id,
                            "title": f"Группа {group_id}",
                            "status": "❓ Ошибка доступа",
                        }
                    )

            # Пагинация
            total_pages = (
                (len(active_groups) - 1) // self.page_size + 1 if active_groups else 1
            )
            page = max(0, min(page, total_pages - 1)) if total_pages > 0 else 0

            start_idx = page * self.page_size
            end_idx = min(start_idx + self.page_size, len(active_groups))
            current_groups = active_groups[start_idx:end_idx] if active_groups else []

            # Создаем сообщение
            message = "📊 Активные мониторинги:\n\n"

            # Добавляем группы в сообщение
            for idx, group in enumerate(current_groups, 1):
                message += (
                    f"{idx}. 📢 {group['title']} (ID: {group['id']}) {group['status']}\n"
                )

            # Создаем кнопки навигации
            buttons = []
            nav_buttons = []

            # Кнопка "Назад"
            if page > 0:
                nav_buttons.append(Button.inline("◀️ Назад", f"mon_page_{page-1}"))

            # Информация о текущей странице
            if total_pages > 1:
                page_info = f"📄 {page+1}/{total_pages}"
                nav_buttons.append(Button.inline(page_info, "dummy"))

            # Кнопка "Вперед"
            if page < total_pages - 1:
                nav_buttons.append(Button.inline("Вперед ▶️", f"mon_page_{page+1}"))

            if nav_buttons:
                buttons.append(nav_buttons)

            # Добавляем кнопку проверки доступности
            buttons.append(
                [Button.inline("🔄 Проверить доступность", "check_availability")]
            )
            buttons.append([Button.inline("🔙 Главное меню", "main_menu")])

            if edit and hasattr(event, "edit") and callable(event.edit):
                try:
                    await event.edit(message, buttons=buttons)
                except:
                    await event.respond(message, buttons=buttons)
            else:
                await event.respond(message, buttons=buttons)
        except Exception as e:
            logger.error(f"Ошибка при показе активных мониторингов: {e}")
            await event.respond(
                "Произошла ошибка при получении списка активных мониторингов."
            )

    async def show_help(self, event):
        """Показывает справку по использованию бота"""
        help_text = """
📌 Как использовать бота:

1️⃣ Нажмите "📋 Список групп" чтобы увидеть все доступные группы
2️⃣ Используйте "🔍 Поиск групп" для поиска по названию
3️⃣ Нажмите на название группы, чтобы включить/отключить мониторинг
4️⃣ Используйте "🔍 Активные мониторинги" для просмотра текущих мониторингов
5️⃣ Используйте "🔄 Проверить доступность" для проверки доступа к группам

⚡️ Когда мониторинг включен:
- Бот будет отмечать сообщения как прочитанные сразу при их получении
- Через 2 минуты после сообщения будет добавлена реакция 👀

💡 Если реакции перестали работать:
- Проверьте доступность групп через меню
- Убедитесь, что у вас остался доступ к группе/каналу
- Возможно, в группе отключены реакции администратором
            """
        buttons = [[Button.inline("🔙 Главное меню", "main_menu")]]
        await event.respond(help_text, buttons=buttons)

    async def toggle_group_monitoring(self, event, data):
        """Обрабатывает включение/отключение мониторинга группы"""
        try:
            group_id = data.split("_")[1]

            # Проверяем, мониторится ли группа
            is_monitoring, monitor_id = self.is_group_monitored(group_id)

            if is_monitoring:
                # Отключаем мониторинг
                if monitor_id:
                    await self.stop_monitoring(monitor_id)
                    # Получаем entity
                    entity = await self.get_entity_safely(group_id)

                    if entity and hasattr(entity, "title"):
                        await event.respond(
                            f"❌ Мониторинг группы '{entity.title}' отключен"
                        )
                    else:
                        await event.respond(
                            f"❌ Мониторинг группы с ID {group_id} отключен"
                        )

                    logger.info(f"Отключен мониторинг группы {monitor_id}")
            else:
                # Включаем мониторинг
                try:
                    # Получаем entity
                    entity = await self.get_entity_safely(group_id)

                    if not entity:
                        await event.respond(
                            f"❌ Не удалось получить доступ к группе с ID {group_id}."
                        )
                        logger.error(
                            f"Не удалось получить entity для группы {group_id}"
                        )
                        return

                    # Получаем актуальный ID группы и нормализуем его
                    actual_group_id = str(entity.id)
                    normalized_id = self.normalize_group_id(actual_group_id)

                    # Сохраняем группу в список мониторинга
                    self.monitored_groups[normalized_id] = True
                    self.save_settings()
                    logger.info(
                        f"Включен мониторинг группы {normalized_id} (исходный ID: {actual_group_id})"
                    )

                    await event.respond(
                        f"✅ Мониторинг группы '{entity.title}' успешно включен!\n"
                        f"Теперь я буду автоматически отмечать сообщения как прочитанные и "
                        f"ставить реакцию 👀 на новые сообщения в этой группе через 2 минуты."
                    )
                    logger.info(
                        f"Подтверждено добавление группы {normalized_id} в мониторинг"
                    )

                except Exception as e:
                    logger.error(
                        f"Ошибка при включении мониторинга группы {group_id}: {e}"
                    )
                    await event.respond(f"❌ Ошибка при включении мониторинга: {str(e)}")

            # Возвращаемся на текущую страницу списка групп
            search = self.search_query.get(event.chat_id)
            page = 0  # Возвращаемся на первую страницу
            await self.show_groups_list(event, page=page, search=search)

        except Exception as e:
            logger.error(f"Ошибка в toggle_group_monitoring: {e}")
            await event.respond("❌ Произошла ошибка. Попробуйте позже.")

    async def stop_monitoring(self, group_id):
        """Останавливает мониторинг группы"""
        if str(group_id) in self.monitored_groups:
            del self.monitored_groups[str(group_id)]
            self.save_settings()
            logger.info(f"Остановлен мониторинг группы {group_id}")
            return True
        return False

    async def ensure_connection(self):
        """Проверяет соединение и восстанавливает его при необходимости"""
        while True:
            try:
                # Проверяем, что клиент подключен
                if not self.client.is_connected():
                    logger.warning("Основной клиент не подключен, переподключаемся...")
                    await self.client.connect()

                # Проверяем авторизацию
                if not await self.client.is_user_authorized():
                    logger.error("Основной клиент потерял авторизацию!")

                    # Уведомляем владельца
                    try:
                        if self.owner_id:
                            await self.bot.send_message(
                                self.owner_id,
                                "⚠️ Внимание! Основной клиент потерял авторизацию. Возможно, потребуется переавторизация.",
                            )
                    except Exception as notify_error:
                        logger.error(
                            f"Не удалось уведомить владельца о потере авторизации: {notify_error}"
                        )

                # Проверяем подключение бота
                if not self.bot.is_connected():
                    logger.warning("Бот не подключен, переподключаемся...")
                    await self.bot.connect()

            except Exception as e:
                logger.error(f"Ошибка при проверке соединения: {e}")

            # Проверяем раз в минуту
            await asyncio.sleep(60)

    # Метод для периодической очистки списка обработанных сообщений
    async def cleanup_processed_messages(self):
        """Периодически очищает список обработанных сообщений"""
        while True:
            try:
                if len(self.processed_messages) > 1000:
                    logger.info(
                        f"Очистка списка обработанных сообщений, текущий размер: {len(self.processed_messages)}"
                    )
                    self.processed_messages = set(list(self.processed_messages)[-1000:])
                    logger.info(f"Размер после очистки: {len(self.processed_messages)}")
            except Exception as e:
                logger.error(f"Ошибка при очистке списка обработанных сообщений: {e}")

            # Очищаем раз в час
            await asyncio.sleep(3600)

    # Метод для периодической проверки доступности групп
    async def periodic_group_validation(self):
        """Периодически проверяет доступность отслеживаемых групп"""
        while True:
            try:
                logger.info("Запуск периодической проверки доступности групп")
                await self.validate_monitored_groups()
                logger.info("Периодическая проверка доступности групп завершена")
            except Exception as e:
                logger.error(f"Ошибка при периодической проверке групп: {e}")

            # Проверяем каждые 12 часов
            await asyncio.sleep(12 * 3600)


async def main():
    try:
        monitor = MessageMonitor()
        await monitor.start()

        if monitor.client and monitor.bot:
            # Запускаем задачи для проверки соединения и очистки списков
            connection_check = asyncio.create_task(monitor.ensure_connection())
            cleanup_task = asyncio.create_task(monitor.cleanup_processed_messages())
            validation_task = asyncio.create_task(monitor.periodic_group_validation())

            # Держим клиенты запущенными
            await asyncio.gather(
                monitor.client.run_until_disconnected(),
                monitor.bot.run_until_disconnected(),
            )

            # Отменяем задачи при завершении
            connection_check.cancel()
            cleanup_task.cancel()
            validation_task.cancel()
        else:
            logger.error(
                "Не удалось инициализировать клиентов. Проверьте настройки подключения."
            )
    except Exception as e:
        logger.error(f"Критическая ошибка в main(): {str(e)}")


if __name__ == "__main__":
    try:
        # Настраиваем обработку исключений для ротации логов
        handler = logging.FileHandler("bot.log")
        handler.setFormatter(
            logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
        )
        logger.addHandler(handler)

        # Добавляем ротацию логов - создаем новый файл каждый день
        from logging.handlers import TimedRotatingFileHandler

        file_handler = TimedRotatingFileHandler(
            "bot.log", when="midnight", interval=1, backupCount=7
        )
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
        )
        logger.addHandler(file_handler)

        # Настраиваем обработку необработанных исключений
        def handle_exception(exc_type, exc_value, exc_traceback):
            if issubclass(exc_type, KeyboardInterrupt):
                sys.__excepthook__(exc_type, exc_value, exc_traceback)
                return
            logger.critical(
                "Необработанное исключение:",
                exc_info=(exc_type, exc_value, exc_traceback),
            )

        import sys

        sys.excepthook = handle_exception

        # Запускаем бота
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот остановлен по команде пользователя")
    except Exception as e:
        logger.error(f"Неожиданная ошибка при запуске бота: {str(e)}")
