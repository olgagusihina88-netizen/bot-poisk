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

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

JSON_PATH = "data.json"

bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
client = AsyncOpenAI(api_key=OPENAI_API_KEY)

class SearchStates(StatesGroup):
    waiting_for_query = State()
    waiting_for_city = State()
    waiting_for_district = State()

def load_data():
    with open(JSON_PATH, 'r', encoding='utf-8') as f:
        return json.load(f)

@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await message.reply("Привет! Я помогу найти лучшие предложения. Напиши, что ищешь (например, 'цветы').")
    await state.set_state(SearchStates.waiting_for_query)

@dp.message(SearchStates.waiting_for_query)
async def process_query(message: types.Message, state: FSMContext):
    await state.update_data(query=message.text.strip().lower())
    await message.reply("В каком городе ищем?")
    await state.set_state(SearchStates.waiting_for_city)

@dp.message(SearchStates.waiting_for_city)
async def process_city(message: types.Message, state: FSMContext):
    await state.update_data(city=message.text.strip().lower())
    await message.reply("Укажите район (или отправьте '-', если не важно):")
    await state.set_state(SearchStates.waiting_for_district)

@dp.message(SearchStates.waiting_for_district)
async def process_district(message: types.Message, state: FSMContext):
    district_input = message.text.strip()
    district = None if district_input == '-' else district_input.lower()

    data = await state.get_data()
    query = data['query']
    city = data['city']

    try:
        items = load_data()
    except Exception as e:
        await message.reply("Ошибка загрузки данных. Попробуйте позже.")
        await state.clear()
        return

    filtered = [item for item in items if 
                query in item.get('category', '').lower() or 
                query in item.get('business_name', '').lower()]

    if not filtered:
        await message.reply("Ничего не найдено по вашему запросу.")
        await state.clear()
        return

    city_filtered = [item for item in filtered if item.get('city', '').lower() == city]
    if not city_filtered:
        await message.reply(f"В городе {city} ничего не найдено.")
        await state.clear()
        return

    if district:
        district_filtered = [item for item in city_filtered if item.get('district', '').lower() == district]
        if district_filtered:
            results = district_filtered
        else:
            await message.reply(f"В районе {district} ничего нет. Показываю по всему городу {city}.")
            results = city_filtered
    else:
        results = city_filtered

    today = datetime.now().date().isoformat()
    paid = [item for item in results if item.get('paid_until') and item['paid_until'] > today]
    unpaid = [item for item in results if not (item.get('paid_until') and item['paid_until'] > today)]
    paid.sort(key=lambda x: x.get('rating', 0), reverse=True)
    unpaid.sort(key=lambda x: x.get('rating', 0), reverse=True)
    sorted_results = paid + unpaid

    candidates = sorted_results[:5]

    if not candidates:
        await message.reply("Нет подходящих предложений.")
        await state.clear()
        return

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
        answer = "Вот что нашлось (без анализа ИИ):\n\n"
        for item in candidates[:3]:
            answer += f"🏆 {item['business_name']}\n📝 {item['description']}\n💰 {item['price']} руб\n⭐ {item.get('rating', '—')}\n🔗 {item['link']}\n\n"

    await message.reply(answer)
    await state.clear()

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
