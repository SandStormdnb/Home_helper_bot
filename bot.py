import asyncio
import csv
from io import StringIO
from datetime import datetime, timedelta, date
from aiogram import Bot, Dispatcher, types
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy.orm import joinedload
from database import SessionLocal, Task, Category

API_TOKEN = "YOUR_TOKEN"
bot = Bot(token=API_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(bot, storage=storage)
scheduler = AsyncIOScheduler()

# ---------- Состояния FSM ----------
class AddTask(StatesGroup):
    title = State()
    category = State()          # выбор/создание категории
    start_date = State()        # дата первого напоминания
    time = State()             # время напоминания
    repeat_type = State()      # none, daily, weekly, interval
    repeat_days = State()      # для weekly
    interval_days = State()    # для interval
    reminder_offset = State()  # доп.напоминание (минут до)

class EditTask(StatesGroup):
    choose_field = State()
    new_value = State()

class AddCategory(StatesGroup):
    name = State()

class RenameCategory(StatesGroup):
    new_name = State()

# ---------- Вспомогательные функции ----------
def validate_time(time_str):
    try:
        datetime.strptime(time_str, "%H:%M")
        return True
    except ValueError:
        return False

def validate_date(date_str):
    try:
        datetime.strptime(date_str, "%d.%m.%Y")
        return True
    except ValueError:
        return False

def get_repeat_text(task: Task):
    if task.repeat_type == 'daily':
        return "Ежедневно"
    elif task.repeat_type == 'weekly' and task.repeat_days:
        days_map = {'mon':'Пн', 'tue':'Вт', 'wed':'Ср', 'thu':'Чт', 'fri':'Пт', 'sat':'Сб', 'sun':'Вс'}
        days = [days_map[d] for d in task.repeat_days.split(',') if d in days_map]
        return "По дням: " + ', '.join(days)
    elif task.repeat_type == 'interval' and task.interval_days:
        return f"Каждые {task.interval_days} дн."
    else:
        return "Однократно"

# ---------- Планирование напоминаний ----------
async def schedule_task(task: Task):
    """Планирует основное и дополнительное (если есть) напоминания"""
    hour, minute = map(int, task.due_time.split(':'))

    # Основное напоминание
    scheduler.add_job(
        send_reminder,
        trigger=get_trigger(task),
        id=f"task_{task.id}",
        args=[task.user_id, task.id],
        replace_existing=True
    )

    # Дополнительное напоминание (за reminder_offset минут)
    if task.reminder_offset and task.reminder_offset > 0:
        offset_hour = hour
        offset_minute = minute - task.reminder_offset
        if offset_minute < 0:
            offset_hour -= 1
            offset_minute += 60
        if offset_hour < 0:
            offset_hour = 23  # предыдущий день – для простоты корректируем так
        scheduler.add_job(
            send_early_reminder,
            trigger=get_trigger(task, offset=True),
            id=f"task_{task.id}_early",
            args=[task.user_id, task.id, task.reminder_offset],
            replace_existing=True
        )

def get_trigger(task: Task, offset=False):
    """Возвращает триггер APScheduler для задачи"""
    hour, minute = map(int, task.due_time.split(':'))
    if offset and task.reminder_offset:
        minute -= task.reminder_offset
        if minute < 0:
            minute += 60
            hour -= 1
        if hour < 0:
            hour = 23

    if task.repeat_type == 'daily':
        return CronTrigger(hour=hour, minute=minute)
    elif task.repeat_type == 'weekly' and task.repeat_days:
        days = task.repeat_days.split(',')
        return CronTrigger(day_of_week=','.join(days), hour=hour, minute=minute)
    elif task.repeat_type == 'interval' and task.interval_days:
        # Первый запуск в start_date в указанное время
        start_datetime = datetime.combine(task.start_date, datetime.min.time()) + timedelta(hours=hour, minutes=minute)
        if start_datetime < datetime.now():
            start_datetime += timedelta(days=task.interval_days)  # если уже прошло, переносим
        return IntervalTrigger(days=task.interval_days, start_date=start_datetime)
    else:
        # Однократная задача: запуск в start_date в указанное время
        run_date = datetime.combine(task.start_date, datetime.min.time()) + timedelta(hours=hour, minutes=minute)
        if run_date < datetime.now():
            run_date += timedelta(days=1)  # переносим на завтра, если время уже прошло
        return CronTrigger(year=run_date.year, month=run_date.month, day=run_date.day,
                           hour=run_date.hour, minute=run_date.minute)

async def send_reminder(user_id, task_id):
    session = SessionLocal()
    task = session.query(Task).options(joinedload(Task.category)).filter(Task.id == task_id).first()
    if task and not task.is_done:
        text = f"⏰ Напоминание: {task.title}\n"
        if task.category:
            text += f"📁 {task.category.name}\n"
        text += f"🕒 {task.due_time}"
        keyboard = InlineKeyboardMarkup().add(
            InlineKeyboardButton("✅ Выполнено", callback_data=f"done_{task.id}")
        )
        await bot.send_message(user_id, text, reply_markup=keyboard)

        # Если интервальная задача – автоматически создаём следующее напоминание (задача остаётся активной)
        if task.repeat_type == 'interval' and task.interval_days:
            # планировщик сам сработает по IntervalTrigger, ничего делать не нужно
            pass
    session.close()

async def send_early_reminder(user_id, task_id, offset):
    session = SessionLocal()
    task = session.query(Task).filter(Task.id == task_id).first()
    if task and not task.is_done:
        await bot.send_message(
            user_id,
            f"⚠️ Напоминание (за {offset} мин): {task.title} в {task.due_time}"
        )
    session.close()

# ---------- Главное меню ----------
def main_menu():
    keyboard = InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        InlineKeyboardButton("📋 Добавить задачу", callback_data="add"),
        InlineKeyboardButton("📅 Список задач", callback_data="list"),
        InlineKeyboardButton("✅ Отметить выполнение", callback_data="done_menu"),
        InlineKeyboardButton("✏️ Редактировать", callback_data="edit_menu"),
        InlineKeyboardButton("🗑 Удалить", callback_data="delete_menu"),
        InlineKeyboardButton("📊 Статистика", callback_data="stats"),
        InlineKeyboardButton("📁 Категории", callback_data="categories"),
        InlineKeyboardButton("📤 Экспорт", callback_data="export")
    )
    return keyboard

@dp.message_handler(commands=['start'])
async def cmd_start(message: types.Message):
    await message.answer(
        "Привет! Я твой домашний помощник.\n"
        "Выбери действие:",
        reply_markup=main_menu()
    )

# ---------- Добавление задачи ----------
@dp.callback_query_handler(text="add")
async def add_task_start(call: CallbackQuery):
    await call.message.edit_text("Введи название задачи:")
    await AddTask.title.set()
    await call.answer()

@dp.message_handler(state=AddTask.title)
async def add_task_title(message: types.Message, state: FSMContext):
    async with state.proxy() as data:
        data['title'] = message.text
    await AddTask.next()
    # Показываем категории пользователя
    await show_categories_for_choice(message, state)

async def show_categories_for_choice(message: types.Message, state: FSMContext, edit=False):
    """Выводит inline-клавиатуру с категориями пользователя + кнопка новой категории"""
    session = SessionLocal()
    user_id = message.from_user.id
    categories = session.query(Category).filter(Category.user_id == user_id).all()
    session.close()

    keyboard = InlineKeyboardMarkup(row_width=2)
    for cat in categories:
        keyboard.insert(InlineKeyboardButton(cat.name, callback_data=f"cat_{cat.id}"))
    keyboard.add(InlineKeyboardButton("➕ Новая категория", callback_data="cat_new"))
    if edit:
        await message.edit_text("Выбери категорию (или создай новую):", reply_markup=keyboard)
    else:
        await message.answer("Выбери категорию (или создай новую):", reply_markup=keyboard)

@dp.callback_query_handler(lambda c: c.data.startswith('cat_'), state=AddTask.category)
async def add_task_category_choice(call: CallbackQuery, state: FSMContext):
    data = call.data
    if data == "cat_new":
        await call.message.edit_text("Введи название новой категории:")
        await AddCategory.name.set()
        await state.update_data(previous_state='AddTask_category')
        await call.answer()
        return
    else:
        cat_id = int(data.split('_')[1])
        async with state.proxy() as state_data:
            state_data['category_id'] = cat_id
    await call.message.edit_text("Категория выбрана. Теперь укажи дату первого напоминания (в формате ДД.ММ.ГГГГ) или отправь '-' для сегодняшней даты:")
    await AddTask.start_date.set()
    await call.answer()

# Обработчик создания новой категории во время добавления задачи
@dp.message_handler(state=AddCategory.name)
async def add_category_name(message: types.Message, state: FSMContext):
    name = message.text.strip()
    if not name:
        await message.answer("Название не может быть пустым. Попробуй ещё раз:")
        return
    session = SessionLocal()
    user_id = message.from_user.id
    # Проверяем, нет ли уже такой категории у пользователя
    existing = session.query(Category).filter(Category.user_id == user_id, Category.name == name).first()
    if existing:
        cat_id = existing.id
    else:
        new_cat = Category(user_id=user_id, name=name)
        session.add(new_cat)
        session.commit()
        cat_id = new_cat.id
    session.close()
    async with state.proxy() as data:
        data['category_id'] = cat_id
    # Возвращаемся в процесс добавления задачи
    await message.answer(f"Категория «{name}» создана. Теперь укажи дату первого напоминания (ДД.ММ.ГГГГ) или '-' для сегодня:")
    await AddTask.start_date.set()
    await state.update_data(previous_state=None)

@dp.message_handler(state=AddTask.start_date)
async def add_task_start_date(message: types.Message, state: FSMContext):
    text = message.text.strip()
    if text == '-':
        start_date = date.today()
    else:
        if not validate_date(text):
            await message.answer("Неверный формат даты! Используй ДД.ММ.ГГГГ (например 25.12.2024) или '-'")
            return
        start_date = datetime.strptime(text, "%d.%m.%Y").date()
    async with state.proxy() as data:
        data['start_date'] = start_date
    await AddTask.next()
    await message.answer("Введи время напоминания в формате ЧЧ:ММ (например 08:00):")

@dp.message_handler(state=AddTask.time)
async def add_task_time(message: types.Message, state: FSMContext):
    if not validate_time(message.text):
        await message.answer("Неверный формат! Используй ЧЧ:ММ (например 14:30).")
        return
    async with state.proxy() as data:
        data['due_time'] = message.text
    await AddTask.next()
    keyboard = InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        InlineKeyboardButton("🔄 Без повтора", callback_data="repeat_none"),
        InlineKeyboardButton("📆 Ежедневно", callback_data="repeat_daily"),
        InlineKeyboardButton("📅 Еженедельно", callback_data="repeat_weekly"),
        InlineKeyboardButton("🕑 Интервал (дни)", callback_data="repeat_interval")
    )
    await message.answer("Выбери тип повтора:", reply_markup=keyboard)

@dp.callback_query_handler(lambda c: c.data.startswith('repeat_'), state=AddTask.repeat_type)
async def add_task_repeat_type(call: CallbackQuery, state: FSMContext):
    repeat_type = call.data.split('_')[1]  # none, daily, weekly, interval
    async with state.proxy() as data:
        data['repeat_type'] = repeat_type
    if repeat_type == 'weekly':
        await AddTask.repeat_days.set()
        keyboard = InlineKeyboardMarkup(row_width=3)
        days = [('Пн', 'mon'), ('Вт', 'tue'), ('Ср', 'wed'), ('Чт', 'thu'), ('Пт', 'fri'), ('Сб', 'sat'), ('Вс', 'sun')]
        for name, code in days:
            keyboard.add(InlineKeyboardButton(name, callback_data=f"day_{code}"))
        keyboard.add(InlineKeyboardButton("✅ Готово", callback_data="days_done"))
        await call.message.edit_text("Выбери дни недели (можно несколько):", reply_markup=keyboard)
        data['repeat_days'] = []
    elif repeat_type == 'interval':
        await AddTask.interval_days.set()
        await call.message.edit_text("Введи интервал в днях (целое число):")
    else:
        # none или daily
        await AddTask.reminder_offset.set()
        await call.message.edit_text("Введи дополнительное напоминание (за сколько минут до времени) или 0, если не нужно:")
    await call.answer()

@dp.callback_query_handler(lambda c: c.data.startswith('day_'), state=AddTask.repeat_days)
async def add_task_repeat_days_select(call: CallbackQuery, state: FSMContext):
    async with state.proxy() as data:
        day = call.data[4:]
        if 'repeat_days' not in data:
            data['repeat_days'] = []
        if day in data['repeat_days']:
            data['repeat_days'].remove(day)
            await call.answer(f"День {day} удалён")
        else:
            data['repeat_days'].append(day)
            await call.answer(f"День {day} добавлен")

@dp.callback_query_handler(text="days_done", state=AddTask.repeat_days)
async def add_task_repeat_days_done(call: CallbackQuery, state: FSMContext):
    async with state.proxy() as data:
        if not data.get('repeat_days'):
            await call.answer("Выбери хотя бы один день!", show_alert=True)
            return
        data['repeat_days'] = ','.join(data['repeat_days'])
    await AddTask.reminder_offset.set()
    await call.message.edit_text("Введи дополнительное напоминание (за сколько минут до времени) или 0, если не нужно:")

@dp.message_handler(state=AddTask.interval_days)
async def add_task_interval_days(message: types.Message, state: FSMContext):
    try:
        days = int(message.text)
        if days <= 0:
            raise ValueError
    except ValueError:
        await message.answer("Введи целое положительное число дней:")
        return
    async with state.proxy() as data:
        data['interval_days'] = days
    await AddTask.reminder_offset.set()
    await message.answer("Введи дополнительное напоминание (за сколько минут до времени) или 0, если не нужно:")

@dp.message_handler(state=AddTask.reminder_offset)
async def add_task_reminder_offset(message: types.Message, state: FSMContext):
    try:
        offset = int(message.text)
        if offset < 0:
            raise ValueError
    except ValueError:
        await message.answer("Введи целое неотрицательное число минут:")
        return
    async with state.proxy() as data:
        data['reminder_offset'] = offset
    # Сохраняем задачу
    await save_task(message, state, message.from_user.id)
    await message.answer("Задача добавлена!", reply_markup=main_menu())

async def save_task(message, state, user_id):
    async with state.proxy() as data:
        session = SessionLocal()
        task = Task(
            user_id=user_id,
            title=data['title'],
            category_id=data.get('category_id'),
            start_date=data.get('start_date', date.today()),
            due_time=data['due_time'],
            repeat_type=data.get('repeat_type', 'none'),
            repeat_days=data.get('repeat_days', None),
            interval_days=data.get('interval_days', 0),
            reminder_offset=data.get('reminder_offset', 0)
        )
        session.add(task)
        session.commit()
        # Планируем напоминания
        await schedule_task(task)
        session.close()
    await state.finish()

# ---------- Список задач с фильтром по категориям ----------
@dp.callback_query_handler(text="list")
async def show_list(call: CallbackQuery):
    await show_categories_filter(call, action="list_filter")

async def show_categories_filter(call: CallbackQuery, action="list_filter"):
    """Показывает категории для фильтрации перед списком задач"""
    session = SessionLocal()
    user_id = call.from_user.id
    categories = session.query(Category).filter(Category.user_id == user_id).all()
    session.close()
    keyboard = InlineKeyboardMarkup(row_width=2)
    keyboard.add(InlineKeyboardButton("📋 Все задачи", callback_data=f"{action}_all"))
    for cat in categories:
        keyboard.add(InlineKeyboardButton(cat.name, callback_data=f"{action}_{cat.id}"))
    await call.message.edit_text("Выбери категорию для просмотра:", reply_markup=keyboard)
    await call.answer()

@dp.callback_query_handler(lambda c: c.data.startswith('list_filter_'))
async def list_tasks_by_category(call: CallbackQuery):
    parts = call.data.split('_')
    cat_filter = parts[2]  # 'all' или id
    user_id = call.from_user.id
    session = SessionLocal()
    query = session.query(Task).options(joinedload(Task.category)).filter(
        Task.user_id == user_id,
        Task.is_done == False
    )
    if cat_filter != 'all':
        query = query.filter(Task.category_id == int(cat_filter))
    tasks = query.order_by(Task.due_time).all()
    session.close()

    if not tasks:
        await call.message.edit_text("В этой категории нет активных задач.", reply_markup=main_menu())
        await call.answer()
        return

    await call.message.delete()
    for task in tasks:
        repeat_info = get_repeat_text(task)
        cat_name = task.category.name if task.category else "Без категории"
        keyboard = InlineKeyboardMarkup(row_width=3)
        keyboard.add(
            InlineKeyboardButton("✅", callback_data=f"done_{task.id}"),
            InlineKeyboardButton("✏️", callback_data=f"edit_{task.id}"),
            InlineKeyboardButton("🗑", callback_data=f"delete_{task.id}")
        )
        await call.message.answer(
            f"🆔 {task.id}\n"
            f"📌 {task.title}\n"
            f"📁 {cat_name}\n"
            f"⏰ {task.due_time}\n"
            f"🔄 {repeat_info}",
            reply_markup=keyboard
        )
    await call.message.answer("Вот твои задачи:", reply_markup=main_menu())
    await call.answer()

# ---------- Отметка о выполнении ----------
@dp.callback_query_handler(lambda c: c.data.startswith('done_'))
async def mark_done(call: CallbackQuery):
    task_id = int(call.data.split('_')[1])
    session = SessionLocal()
    task = session.query(Task).filter(Task.id == task_id, Task.user_id == call.from_user.id).first()
    if task:
        task.is_done = True
        session.commit()
        # Удаляем запланированные напоминания
        scheduler.remove_job(f"task_{task.id}")
        scheduler.remove_job(f"task_{task.id}_early")
        await call.message.edit_text(f"✅ Задача «{task.title}» выполнена!")
    else:
        await call.answer("Задача не найдена", show_alert=True)
    session.close()
    await call.answer()

@dp.callback_query_handler(text="done_menu")
async def done_menu(call: CallbackQuery):
    await show_categories_filter(call, action="done_filter")

@dp.callback_query_handler(lambda c: c.data.startswith('done_filter_'))
async def done_tasks_by_category(call: CallbackQuery):
    parts = call.data.split('_')
    cat_filter = parts[2]
    user_id = call.from_user.id
    session = SessionLocal()
    query = session.query(Task).filter(
        Task.user_id == user_id,
        Task.is_done == False
    )
    if cat_filter != 'all':
        query = query.filter(Task.category_id == int(cat_filter))
    tasks = query.order_by(Task.due_time).all()
    session.close()
    if not tasks:
        await call.message.edit_text("Нет задач для отметки.", reply_markup=main_menu())
        await call.answer()
        return
    await call.message.delete()
    for task in tasks:
        keyboard = InlineKeyboardMarkup().add(
            InlineKeyboardButton("✅ Выполнить", callback_data=f"done_{task.id}")
        )
        await call.message.answer(f"🆔 {task.id} – {task.title}", reply_markup=keyboard)
    await call.message.answer("Выбери задачу:", reply_markup=main_menu())
    await call.answer()

# ---------- Редактирование ----------
@dp.callback_query_handler(text="edit_menu")
async def edit_menu(call: CallbackQuery):
    await show_categories_filter(call, action="edit_filter")

@dp.callback_query_handler(lambda c: c.data.startswith('edit_filter_'))
async def edit_tasks_by_category(call: CallbackQuery):
    parts = call.data.split('_')
    cat_filter = parts[2]
    user_id = call.from_user.id
    session = SessionLocal()
    query = session.query(Task).filter(
        Task.user_id == user_id,
        Task.is_done == False
    )
    if cat_filter != 'all':
        query = query.filter(Task.category_id == int(cat_filter))
    tasks = query.all()
    session.close()
    if not tasks:
        await call.message.edit_text("Нет задач для редактирования.", reply_markup=main_menu())
        await call.answer()
        return
    await call.message.delete()
    for task in tasks:
        keyboard = InlineKeyboardMarkup().add(
            InlineKeyboardButton("✏️ Редактировать", callback_data=f"edit_{task.id}")
        )
        await call.message.answer(f"🆔 {task.id} – {task.title}", reply_markup=keyboard)
    await call.message.answer("Выбери задачу:", reply_markup=main_menu())
    await call.answer()

@dp.callback_query_handler(lambda c: c.data.startswith('edit_'))
async def choose_edit_field(call: CallbackQuery, state: FSMContext):
    task_id = int(call.data.split('_')[1])
    await state.update_data(task_id=task_id)
    keyboard = InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        InlineKeyboardButton("Название", callback_data="field_title"),
        InlineKeyboardButton("Время", callback_data="field_time"),
        InlineKeyboardButton("Категорию", callback_data="field_category"),
        InlineKeyboardButton("Повтор", callback_data="field_repeat"),
        InlineKeyboardButton("Доп.напоминание", callback_data="field_offset")
    )
    await call.message.edit_text("Что изменить?", reply_markup=keyboard)
    await EditTask.choose_field.set()
    await call.answer()

@dp.callback_query_handler(lambda c: c.data.startswith('field_'), state=EditTask.choose_field)
async def edit_field(call: CallbackQuery, state: FSMContext):
    field = call.data[6:]  # title, time, category, repeat, offset
    await state.update_data(field=field)
    if field == 'category':
        # Показываем выбор категории
        await show_categories_for_choice(call.message, state, edit=True)
        await EditTask.new_value.set()
    elif field == 'repeat':
        keyboard = InlineKeyboardMarkup(row_width=2)
        keyboard.add(
            InlineKeyboardButton("🔄 Без повтора", callback_data="repeat_none"),
            InlineKeyboardButton("📆 Ежедневно", callback_data="repeat_daily"),
            InlineKeyboardButton("📅 Еженедельно", callback_data="repeat_weekly"),
            InlineKeyboardButton("🕑 Интервал (дни)", callback_data="repeat_interval")
        )
        await call.message.edit_text("Выбери новый тип повтора:", reply_markup=keyboard)
        await EditTask.new_value.set()
    elif field == 'offset':
        await call.message.edit_text("Введи новое значение доп.напоминания (минут до, 0 - отключить):")
        await EditTask.new_value.set()
    else:
        prompts = {
            'title': "Введи новое название:",
            'time': "Введи новое время (ЧЧ:ММ):"
        }
        await call.message.edit_text(prompts[field])
        await EditTask.new_value.set()
    await call.answer()

@dp.message_handler(state=EditTask.new_value)
async def edit_new_value_text(message: types.Message, state: FSMContext):
    data = await state.get_data()
    field = data['field']
    task_id = data['task_id']
    session = SessionLocal()
    task = session.query(Task).filter(Task.id == task_id, Task.user_id == message.from_user.id).first()
    if not task:
        await message.answer("Задача не найдена.")
        await state.finish()
        session.close()
        return

    if field == 'title':
        task.title = message.text
    elif field == 'time':
        if not validate_time(message.text):
            await message.answer("Неверный формат! Попробуй ещё раз.")
            return
        task.due_time = message.text
    elif field == 'offset':
        try:
            offset = int(message.text)
            if offset < 0: raise ValueError
            task.reminder_offset = offset
        except:
            await message.answer("Введи целое неотрицательное число.")
            return

    session.commit()
    # Перепланируем напоминания
    await schedule_task(task)
    await message.answer("Задача обновлена!", reply_markup=main_menu())
    await state.finish()
    session.close()

@dp.callback_query_handler(lambda c: c.data.startswith('repeat_'), state=EditTask.new_value)
async def edit_repeat_type(call: CallbackQuery, state: FSMContext):
    repeat_type = call.data.split('_')[1]
    data = await state.get_data()
    task_id = data['task_id']
    session = SessionLocal()
    task = session.query(Task).filter(Task.id == task_id, Task.user_id == call.from_user.id).first()
    if task:
        task.repeat_type = repeat_type
        task.repeat_days = None
        task.interval_days = 0
        if repeat_type == 'weekly':
            # Нужно будет задать дни – упростим: по умолчанию пн-пт
            task.repeat_days = 'mon,tue,wed,thu,fri'
        elif repeat_type == 'interval':
            task.interval_days = 1  # по умолчанию 1 день
        session.commit()
        await schedule_task(task)
    session.close()
    await call.message.edit_text("Тип повтора обновлён!", reply_markup=main_menu())
    await state.finish()
    await call.answer()

@dp.callback_query_handler(lambda c: c.data.startswith('cat_'), state=EditTask.new_value)
async def edit_category_choice(call: CallbackQuery, state: FSMContext):
    data = call.data
    if data == "cat_new":
        await call.message.edit_text("Введи название новой категории:")
        await AddCategory.name.set()
        await state.update_data(previous_state='EditTask_new_value')
        await call.answer()
        return
    else:
        cat_id = int(data.split('_')[1])
        state_data = await state.get_data()
        task_id = state_data['task_id']
        session = SessionLocal()
        task = session.query(Task).filter(Task.id == task_id, Task.user_id == call.from_user.id).first()
        if task:
            task.category_id = cat_id
            session.commit()
            await schedule_task(task)
        session.close()
        await call.message.edit_text("Категория обновлена!", reply_markup=main_menu())
        await state.finish()
        await call.answer()

# ---------- Удаление ----------
@dp.callback_query_handler(text="delete_menu")
async def delete_menu(call: CallbackQuery):
    await show_categories_filter(call, action="delete_filter")

@dp.callback_query_handler(lambda c: c.data.startswith('delete_filter_'))
async def delete_tasks_by_category(call: CallbackQuery):
    parts = call.data.split('_')
    cat_filter = parts[2]
    user_id = call.from_user.id
    session = SessionLocal()
    query = session.query(Task).filter(
        Task.user_id == user_id,
        Task.is_done == False
    )
    if cat_filter != 'all':
        query = query.filter(Task.category_id == int(cat_filter))
    tasks = query.all()
    session.close()
    if not tasks:
        await call.message.edit_text("Нет задач для удаления.", reply_markup=main_menu())
        await call.answer()
        return
    await call.message.delete()
    for task in tasks:
        keyboard = InlineKeyboardMarkup().add(
            InlineKeyboardButton("🗑 Удалить", callback_data=f"delete_{task.id}")
        )
        await call.message.answer(f"🆔 {task.id} – {task.title}", reply_markup=keyboard)
    await call.message.answer("Выбери задачу для удаления:", reply_markup=main_menu())
    await call.answer()

@dp.callback_query_handler(lambda c: c.data.startswith('delete_'))
async def confirm_delete(call: CallbackQuery):
    task_id = int(call.data.split('_')[1])
    keyboard = InlineKeyboardMarkup()
    keyboard.add(
        InlineKeyboardButton("Да", callback_data=f"confirm_delete_{task_id}"),
        InlineKeyboardButton("Нет", callback_data="cancel_delete")
    )
    await call.message.edit_text("Точно удалить задачу?", reply_markup=keyboard)
    await call.answer()

@dp.callback_query_handler(lambda c: c.data.startswith('confirm_delete_'))
async def delete_task(call: CallbackQuery):
    task_id = int(call.data.split('_')[2])
    session = SessionLocal()
    task = session.query(Task).filter(Task.id == task_id, Task.user_id == call.from_user.id).first()
    if task:
        # Удаляем напоминания
        scheduler.remove_job(f"task_{task.id}")
        scheduler.remove_job(f"task_{task.id}_early")
        session.delete(task)
        session.commit()
        await call.message.edit_text("Задача удалена.")
    else:
        await call.answer("Задача не найдена", show_alert=True)
    session.close()
    await call.answer()

@dp.callback_query_handler(text="cancel_delete")
async def cancel_delete(call: CallbackQuery):
    await call.message.edit_text("Удаление отменено.", reply_markup=main_menu())
    await call.answer()

# ---------- Управление категориями ----------
@dp.callback_query_handler(text="categories")
async def categories_menu(call: CallbackQuery):
    user_id = call.from_user.id
    session = SessionLocal()
    categories = session.query(Category).filter(Category.user_id == user_id).all()
    session.close()
    if not categories:
        text = "У тебя пока нет категорий. Создай первую!"
    else:
        text = "Твои категории:"
    keyboard = InlineKeyboardMarkup(row_width=1)
    for cat in categories:
        keyboard.add(
            InlineKeyboardButton(f"📁 {cat.name}", callback_data=f"cat_view_{cat.id}"),
            InlineKeyboardButton(f"✏️ Переименовать {cat.name}", callback_data=f"cat_rename_{cat.id}"),
            InlineKeyboardButton(f"🗑 Удалить {cat.name}", callback_data=f"cat_delete_{cat.id}")
        )
    keyboard.add(InlineKeyboardButton("➕ Новая категория", callback_data="cat_new_main"))
    keyboard.add(InlineKeyboardButton("◀️ Назад", callback_data="back_main"))
    await call.message.edit_text(text, reply_markup=keyboard)
    await call.answer()

@dp.callback_query_handler(text="cat_new_main")
async def add_category_main(call: CallbackQuery):
    await call.message.edit_text("Введи название новой категории:")
    await AddCategory.name.set()
    await state.update_data(previous_state='categories_menu')
    await call.answer()

# (обработчик AddCategory.name уже есть выше, он универсальный)

@dp.callback_query_handler(lambda c: c.data.startswith('cat_rename_'))
async def rename_category_start(call: CallbackQuery, state: FSMContext):
    cat_id = int(call.data.split('_')[2])
    await state.update_data(cat_id=cat_id)
    await call.message.edit_text("Введи новое название категории:")
    await RenameCategory.new_name.set()
    await call.answer()

@dp.message_handler(state=RenameCategory.new_name)
async def rename_category(message: types.Message, state: FSMContext):
    new_name = message.text.strip()
    if not new_name:
        await message.answer("Название не может быть пустым.")
        return
    data = await state.get_data()
    cat_id = data['cat_id']
    session = SessionLocal()
    category = session.query(Category).filter(Category.id == cat_id, Category.user_id == message.from_user.id).first()
    if category:
        category.name = new_name
        session.commit()
        await message.answer("Категория переименована!")
    else:
        await message.answer("Категория не найдена.")
    session.close()
    await state.finish()
    # Возвращаемся в меню категорий
    await categories_menu(await fake_call(message))

async def fake_call(message):
    """Создаёт объект CallbackQuery для повторного вызова меню"""
    return CallbackQuery(
        id='fake',
        from_user=message.from_user,
        message=message,
        data='categories',
        chat_instance='fake'
    )

@dp.callback_query_handler(lambda c: c.data.startswith('cat_delete_'))
async def delete_category(call: CallbackQuery):
    cat_id = int(call.data.split('_')[2])
    keyboard = InlineKeyboardMarkup()
    keyboard.add(
        InlineKeyboardButton("Да", callback_data=f"confirm_cat_delete_{cat_id}"),
        InlineKeyboardButton("Нет", callback_data="categories")
    )
    await call.message.edit_text("При удалении категории все задачи в ней останутся без категории. Продолжить?", reply_markup=keyboard)
    await call.answer()

@dp.callback_query_handler(lambda c: c.data.startswith('confirm_cat_delete_'))
async def confirm_delete_category(call: CallbackQuery):
    cat_id = int(call.data.split('_')[3])
    session = SessionLocal()
    category = session.query(Category).filter(Category.id == cat_id, Category.user_id == call.from_user.id).first()
    if category:
        # Убираем связь у задач
        tasks = session.query(Task).filter(Task.category_id == cat_id).all()
        for task in tasks:
            task.category_id = None
        session.delete(category)
        session.commit()
        await call.message.edit_text("Категория удалена.")
    else:
        await call.answer("Категория не найдена", show_alert=True)
    session.close()
    await call.answer()
    # Возвращаемся в меню категорий
    await categories_menu(call)

@dp.callback_query_handler(lambda c: c.data.startswith('cat_view_'))
async def view_category_tasks(call: CallbackQuery):
    cat_id = int(call.data.split('_')[2])
    # Используем фильтр для показа задач
    call.data = f"list_filter_{cat_id}"
    await list_tasks_by_category(call)

# ---------- Статистика ----------
@dp.callback_query_handler(text="stats")
async def show_stats(call: CallbackQuery):
    session = SessionLocal()
    user_id = call.from_user.id
    today = datetime.now().date()
    week_ago = today - timedelta(days=7)
    month_ago = today - timedelta(days=30)

    today_count = session.query(Task).filter(
        Task.user_id == user_id,
        Task.is_done == True,
        Task.created_at >= today
    ).count()

    week_count = session.query(Task).filter(
        Task.user_id == user_id,
        Task.is_done == True,
        Task.created_at >= week_ago
    ).count()

    month_count = session.query(Task).filter(
        Task.user_id == user_id,
        Task.is_done == True,
        Task.created_at >= month_ago
    ).count()

    total_tasks = session.query(Task).filter(Task.user_id == user_id).count()
    active_tasks = session.query(Task).filter(Task.user_id == user_id, Task.is_done == False).count()
    categories_count = session.query(Category).filter(Category.user_id == user_id).count()

    session.close()

    text = (
        f"📊 Статистика:\n\n"
        f"✅ Выполнено сегодня: {today_count}\n"
        f"✅ Выполнено за неделю: {week_count}\n"
        f"✅ Выполнено за месяц: {month_count}\n"
        f"📋 Всего задач: {total_tasks}\n"
        f"🔄 Активных: {active_tasks}\n"
        f"📁 Категорий: {categories_count}"
    )
    await call.message.edit_text(text, reply_markup=main_menu())
    await call.answer()

# ---------- Экспорт статистики в CSV ----------
@dp.callback_query_handler(text="export")
async def export_stats(call: CallbackQuery):
    user_id = call.from_user.id
    session = SessionLocal()
    tasks = session.query(Task).filter(Task.user_id == user_id).order_by(Task.created_at.desc()).all()
    session.close()

    if not tasks:
        await call.answer("Нет данных для экспорта", show_alert=True)
        return

    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(['ID', 'Название', 'Категория', 'Время', 'Дата создания', 'Статус', 'Тип повтора'])
    for task in tasks:
        cat_name = task.category.name if task.category else ''
        status = 'Выполнена' if task.is_done else 'Активна'
        repeat = get_repeat_text(task)
        writer.writerow([
            task.id, task.title, cat_name, task.due_time,
            task.created_at.strftime('%d.%m.%Y %H:%M'),
            status, repeat
        ])

    csv_data = output.getvalue().encode('utf-8-sig')
    await call.message.answer_document(
        types.InputFile(
            csv_data,
            filename=f"tasks_export_{datetime.now().strftime('%Y%m%d')}.csv"
        ),
        caption="Экспорт задач"
    )
    await call.answer()

# ---------- Кнопка "Назад" ----------
@dp.callback_query_handler(text="back_main")
async def back_to_main(call: CallbackQuery):
    await call.message.edit_text("Выбери действие:", reply_markup=main_menu())
    await call.answer()

# ---------- Запуск ----------
async def main():
    scheduler.start()
    await dp.start_polling()

if __name__ == '__main__':
    asyncio.run(main())