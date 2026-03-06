import os
import json
import asyncio
from datetime import datetime
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from openai import AsyncOpenAI
from dotenv import load_dotenv

# Загружаем переменные окружения
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# Путь к файлу с данными
JSON_PATH = "data.json"

bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
client = AsyncOpenAI(api_key=OPENAI_API_KEY)

# Определяем состояния для FSM
class SearchStates(StatesGroup):
    waiting_for_city = State()      # ждём город
    waiting_for_district = State()   # ждём район

# Функция загрузки данных из JSON
def load_data():
    try:
        with open(JSON_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        print(f"Ошибка загрузки JSON: {e}")
        return []

# Хендлер на любое сообщение (не команда)
@dp.message()
async def handle_any_message(message: types.Message, state: FSMContext):
    # Проверяем, находится ли пользователь уже в каком-то состоянии
    current_state = await state.get_state()
    if current_state is None:
        # Пользователь не в диалоге – начинаем новый поиск
        query = message.text.strip().lower()
        if not query:
            await message.reply("Пожалуйста, напишите, что ищете (например, 'цветы').")
            return
        # Сохраняем запрос
        await state.update_data(query=query)
        await message.reply("В каком городе ищем?")
        await state.set_state(SearchStates.waiting_for_city)
    else:
        # Если состояние уже есть – ничего не делаем, обработчики состояний сработают сами
        pass

# Хендлер для состояния ожидания города
@dp.message(SearchStates.waiting_for_city)
async def process_city(message: types.Message, state: FSMContext):
    city = message.text.strip().lower()
    if not city:
        await message.reply("Пожалуйста, укажите город.")
        return
    await state.update_data(city=city)
    await message.reply("Укажите район (или напишите 'любой', если не важно):")
    await state.set_state(SearchStates.waiting_for_district)

# Хендлер для состояния ожидания района
@dp.message(SearchStates.waiting_for_district)
async def process_district(message: types.Message, state: FSMContext):
    district_input = message.text.strip().lower()
    # Если пользователь ввёл "любой", "нет", "-", считаем, что район не важен
    skip_keywords = ["любой", "нет", "-", "pass", "не важно"]
    if district_input in skip_keywords:
        district = None
    else:
        district = district_input

    # Получаем сохранённые данные
    data = await state.get_data()
    query = data['query']
    city = data['city']

    # Загружаем все записи из JSON
    items = load_data()
    if not items:
        await message.reply("Ошибка загрузки базы данных. Попробуйте позже.")
        await state.clear()
        return

    # Фильтруем по категории или названию (запрос)
    filtered = [item for item in items if
                query in item.get('category', '').lower() or
                query in item.get('business_name', '').lower()]

    if not filtered:
        await message.reply("По вашему запросу ничего не найдено.")
        await state.clear()
        return

    # Фильтруем по городу
    city_filtered = [item for item in filtered if item.get('city', '').lower() == city]
    if not city_filtered:
        await message.reply(f"В городе {city} ничего не найдено.")
        await state.clear()
        return

    # Если указан район, фильтруем по нему, иначе оставляем все по городу
    if district:
        district_filtered = [item for item in city_filtered if item.get('district', '').lower() == district]
        if district_filtered:
            results = district_filtered
        else:
            await message.reply(f"В районе {district} ничего нет. Показываю по всему городу {city}.")
            results = city_filtered
    else:
        results = city_filtered

    # Сортируем: сначала платные (paid_until > сегодня), потом по рейтингу
    today = datetime.now().date().isoformat()
    paid = [item for item in results if item.get('paid_until') and item['paid_until'] > today]
    unpaid = [item for item in results if not (item.get('paid_until') and item['paid_until'] > today)]
    paid.sort(key=lambda x: x.get('rating', 0), reverse=True)
    unpaid.sort(key=lambda x: x.get('rating', 0), reverse=True)
    sorted_results = paid + unpaid

    # Берём топ-5 кандидатов для анализа
    candidates = sorted_results[:5]

    if not candidates:
        await message.reply("Нет подходящих предложений.")
        await state.clear()
        return

    # Формируем промпт для OpenAI
    prompt = f"Пользователь ищет: {query} в городе {city}"
    if district:
        prompt += f", район {district}.\n"
    else:
        prompt += ".\n"

    prompt += "Вот список предложений (название, цена, рейтинг, описание, ссылка):\n"
    for i, item in enumerate(candidates, 1):
        prompt += f"{i}. {item['business_name']} — {item['price']} руб. Рейтинг: {item.get('rating', '—')}. Описание: {item['description']}. Ссылка: {item['link']}\n"

    prompt += "\nПроанализируй предложения и выбери одно самое выгодное по соотношению цена/качество. Учти возможные скидки или акции. Посчитай, сколько пользователь сэкономит, выбрав этот вариант, по сравнению со средним по рынку или со вторым по выгодности. Ответ напиши в формате: 'Лучший вариант: [название]. Экономия: [сумма] руб. Подробнее: [краткое обоснование]. Ссылка: [ссылка]'."

    try:
        response = await client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.5,
            max_tokens=400
        )
        answer = response.choices[0].message.content
    except Exception as e:
        # Если OpenAI недоступен, выдаём простой список
        answer = "Вот что нашлось (без анализа ИИ):\n\n"
        for item in candidates[:3]:
            answer += f"🏆 {item['business_name']}\n📝 {item['description']}\n💰 {item['price']} руб\n⭐ {item.get('rating', '—')}\n🔗 {item['link']}\n\n"

    await message.reply(answer)
    await state.clear()

# Команда /start (оставим для справки)
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()  # сбрасываем состояние
    await message.reply("Привет! Просто напишите, что ищете (например, 'цветы').")

# Запуск бота
async def main():
    # Сбрасываем вебхук при старте (на всякий случай)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
