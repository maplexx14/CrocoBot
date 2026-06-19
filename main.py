import asyncio
import random
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from db import check_user, delete_dup, get_info, init_db, plus_ans, reg_db

# ---------------------------------------------------------------------------
# Настройки
# ---------------------------------------------------------------------------
TOKEN = "token"
WORDS_FILE = Path("words.txt")
USERS_FILE = Path("id.txt")
VOTED_FILE = Path("voted_users.txt")
MAX_WORD_INDEX = 2600

# ---------------------------------------------------------------------------
# FSM — состояния для ввода слова
# ---------------------------------------------------------------------------
class WordInput(StatesGroup):
    waiting_for_word = State()


# ---------------------------------------------------------------------------
# Состояние игры (один чат — одна игра, хранится в памяти)
# ---------------------------------------------------------------------------
class GameState:
    def __init__(self):
        self.chat_id: int | None = None
        self.joined_users: set[str] = self._load_set(USERS_FILE)
        self.voted_users: set[str] = self._load_set(VOTED_FILE)
        self.words: list[str] = [
            w.strip().lower()
            for w in WORDS_FILE.read_text(encoding="utf-8").splitlines()
            if w.strip()
        ]
        self.reset()

    # --- persistence helpers ---
    @staticmethod
    def _load_set(path: Path) -> set[str]:
        if not path.exists():
            return set()
        return {line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()}

    # --- game lifecycle ---
    def reset(self):
        self.count_play: int = 0
        self.count_own: int = 0
        self.own_word: str | None = None
        self.word_active: bool = False
        self.current_player: int | None = None
        self.rand_num: int = random.randint(1, MAX_WORD_INDEX - 1)
        self.total_votes: int = 0
        self.count: int = 0

    def new_word(self):
        self.rand_num = random.randint(1, MAX_WORD_INDEX - 1)

    def invalidate_word(self):
        """Стирает текущее слово из массива после угадывания."""
        self.words[self.rand_num] = ""

    # --- logic ---
    def get_current_word(self) -> str:
        return self.words[self.rand_num]

    def is_correct_guess(self, text: str) -> bool:
        candidates: set[str] = {self.words[self.rand_num]}
        if self.own_word:
            candidates.add(self.own_word.strip().lower())
        return text.strip().lower() in candidates

    @property
    def is_running(self) -> bool:
        return self.word_active or self.count_play > 0 or self.count_own > 0


state = GameState()

# ---------------------------------------------------------------------------
# Bot & Dispatcher
# ---------------------------------------------------------------------------
bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())


# ---------------------------------------------------------------------------
# Утилиты
# ---------------------------------------------------------------------------
def mention(user_id: int, first_name: str) -> str:
    return f"[{first_name}](tg://user?id={user_id})"


def game_end_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Начать обычную игру", callback_data="host")],
        [InlineKeyboardButton(text="Загадать своё слово", callback_data="own_new")],
    ])


def host_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Посмотреть слово", callback_data="slovo")],
        [InlineKeyboardButton(text="Следующее слово", callback_data="sled")],
    ])


def own_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Просмотреть слово", callback_data="own")],
    ])


def vote_markup(total: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"Проголосовать ({total})", callback_data="vote")],
    ])


def register_user(user) -> None:
    uid = str(user.id)
    if uid not in state.joined_users:
        with USERS_FILE.open("a", encoding="utf-8") as f:
            f.write(uid + "\n")
        state.joined_users.add(uid)
        reg_db(user_id=user.id, answers=0, first_name=user.first_name)


def finalize_round() -> None:
    state.invalidate_word()
    state.reset()
    delete_dup()


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------
@dp.message(Command("start"))
async def cmd_start(message: Message):
    state.chat_id = message.chat.id
    state.reset()
    register_user(message.from_user)
    init_db()
    await message.answer(
        "/play — начать обычную игру\n"
        "/own — загадать своё слово (пишешь боту в ЛС)\n"
        "/stat — статистика\n"
        "/stop — голосование за смену ведущего"
    )


# ---------------------------------------------------------------------------
# /stat
# ---------------------------------------------------------------------------
@dp.message(Command("stat"))
async def cmd_stat(message: Message):
    if not state.joined_users:
        await message.answer("Пока нет игроков.")
        return
    for uid in state.joined_users:
        try:
            info = get_info(user_id=uid)
            await message.answer(
                f"[{info[2]}](tg://user?id={uid}) — {info[1]} ответов",
                parse_mode="Markdown",
            )
        except Exception as e:
            print(f"Stat error for {uid}: {e}")


# ---------------------------------------------------------------------------
# /stop  — голосование за смену ведущего
# ---------------------------------------------------------------------------
@dp.message(Command("stop"))
async def cmd_stop(message: Message):
    state.voted_users.clear()
    VOTED_FILE.write_text("", encoding="utf-8")
    state.total_votes = 0
    await message.answer(
        "Голосование за смену ведущего",
        reply_markup=vote_markup(0),
    )


# ---------------------------------------------------------------------------
# /play
# ---------------------------------------------------------------------------
@dp.message(Command("play"))
async def cmd_play(message: Message):
    if state.is_running:
        await message.answer("Невозможно начать игру — уже идёт.")
        return

    state.current_player = message.from_user.id
    state.word_active = True
    state.count_play += 1

    await message.answer(
        f"{mention(message.from_user.id, message.from_user.first_name)} загадывает слово",
        parse_mode="Markdown",
        reply_markup=host_markup(),
    )


# ---------------------------------------------------------------------------
# /own
# ---------------------------------------------------------------------------
@dp.message(Command("own"))
async def cmd_own(message: Message, fsm_state: FSMContext):
    if state.is_running:
        await message.answer("Невозможно начать другую игру.")
        return

    state.current_player = message.from_user.id
    state.count_own += 1

    if state.chat_id:
        await bot.send_message(
            state.chat_id,
            f"{mention(message.from_user.id, message.from_user.first_name)} вводит слово боту",
            parse_mode="Markdown",
        )

    # Просим слово в ЛС
    await message.answer("Введи слово, которое хочешь загадать:")
    await fsm_state.set_state(WordInput.waiting_for_word)


# ---------------------------------------------------------------------------
# FSM — получение кастомного слова
# ---------------------------------------------------------------------------
@dp.message(WordInput.waiting_for_word)
async def receive_own_word(message: Message, fsm_state: FSMContext):
    word = message.text.strip() if message.text else ""
    if not word:
        await message.answer("Слово не может быть пустым. Попробуй ещё:")
        return

    state.own_word = word.lower()
    state.word_active = True
    await fsm_state.clear()

    await message.answer("Слово принято!")

    if state.chat_id:
        await bot.send_message(
            state.chat_id,
            f"{mention(message.from_user.id, message.from_user.first_name)} загадывает слово",
            parse_mode="Markdown",
            reply_markup=own_markup(),
        )


# ---------------------------------------------------------------------------
# Проверка угаданного слова (все текстовые сообщения в чате)
# ---------------------------------------------------------------------------
@dp.message(F.text, F.chat.type.in_({"group", "supergroup"}))
async def check_guess(message: Message):
    # Игра не активна или пишет сам ведущий — пропускаем
    if not state.word_active:
        return
    if message.from_user.id == state.current_player:
        return
    if not state.is_correct_guess(message.text):
        return

    # Правильный ответ!
    uid = message.from_user.id
    await message.answer(
        f"{mention(uid, message.from_user.first_name)} отгадал слово *{message.text.lower()}*",
        parse_mode="Markdown",
        reply_markup=game_end_markup(),
    )

    if check_user(user_id=uid) == (0,):
        reg_db(user_id=uid, answers=1, first_name=message.from_user.first_name)
    else:
        plus_ans(answers=1, user_id=uid)

    finalize_round()


# ---------------------------------------------------------------------------
# Callback-кнопки
# ---------------------------------------------------------------------------
@dp.callback_query(F.data == "slovo")
async def cb_slovo(call: CallbackQuery):
    if call.from_user.id != state.current_player:
        await call.answer("Тебе недоступно это слово.", show_alert=True)
        return
    await call.answer(f"Слово: {state.get_current_word()}", show_alert=True)


@dp.callback_query(F.data == "sled")
async def cb_sled(call: CallbackQuery):
    if call.from_user.id != state.current_player:
        await call.answer("Тебе недоступно это слово.", show_alert=True)
        return
    state.new_word()
    await call.answer(f"Новое слово: {state.get_current_word()}", show_alert=True)
    await call.message.answer(
        f"{mention(call.from_user.id, call.from_user.first_name)} решил заменить слово",
        parse_mode="Markdown",
    )


@dp.callback_query(F.data == "own")
async def cb_own_view(call: CallbackQuery):
    if call.from_user.id != state.current_player or not state.own_word:
        await call.answer("Тебе недоступно это слово.", show_alert=True)
        return
    await call.answer(f"Слово: {state.own_word}", show_alert=True)


@dp.callback_query(F.data == "vote")
async def cb_vote(call: CallbackQuery):
    uid_str = str(call.from_user.id)
    if uid_str in state.voted_users:
        await call.answer("Вы уже проголосовали.", show_alert=True)
        return

    state.voted_users.add(uid_str)
    with VOTED_FILE.open("a", encoding="utf-8") as f:
        f.write(uid_str + "\n")
    state.total_votes += 1
    await call.answer("Ваш голос засчитан!", show_alert=True)

    await call.message.edit_text(
        f"Голосование за смену ведущего. Всего голосов: {state.total_votes}",
        reply_markup=vote_markup(state.total_votes),
    )

    member_count = await bot.get_chat_member_count(call.message.chat.id)
    if state.total_votes >= member_count / 2:
        await call.message.answer("Игра остановлена.")
        finalize_round()


@dp.callback_query(F.data == "host")
async def cb_host(call: CallbackQuery):
    if state.count != 0:
        await call.message.answer("Игра уже идёт.")
        return

    state.count += 1
    state.current_player = call.from_user.id
    state.word_active = True
    state.new_word()

    await call.answer("Теперь ты хост!", show_alert=True)
    await call.message.answer(
        f"{mention(call.from_user.id, call.from_user.first_name)} загадывает слово",
        parse_mode="Markdown",
        reply_markup=host_markup(),
    )


@dp.callback_query(F.data == "own_new")
async def cb_own_new(call: CallbackQuery, fsm_state: FSMContext):
    if state.count != 0:
        await call.message.answer("Игра уже идёт.")
        return

    state.count += 1
    state.current_player = call.from_user.id

    await call.answer("Введи своё слово самому боту.", show_alert=True)
    await bot.send_message(call.from_user.id, "Введи слово, которое хочешь загадать:")
    await fsm_state.set_state(WordInput.waiting_for_word)


# ---------------------------------------------------------------------------
# Запуск
# ---------------------------------------------------------------------------
async def main():
    print("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())import asyncio
import random
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from db import check_user, delete_dup, get_info, init_db, plus_ans, reg_db

# ---------------------------------------------------------------------------
# Настройки
# ---------------------------------------------------------------------------
TOKEN = "token"
WORDS_FILE = Path("words.txt")
WORDS_UPPER_FILE = Path("words_upper.txt")
USERS_FILE = Path("id.txt")
VOTED_FILE = Path("voted_users.txt")
MAX_WORD_INDEX = 2600

# ---------------------------------------------------------------------------
# FSM — состояния для ввода слова
# ---------------------------------------------------------------------------
class WordInput(StatesGroup):
    waiting_for_word = State()


# ---------------------------------------------------------------------------
# Состояние игры (один чат — одна игра, хранится в памяти)
# ---------------------------------------------------------------------------
class GameState:
    def __init__(self):
        self.chat_id: int | None = None
        self.joined_users: set[str] = self._load_set(USERS_FILE)
        self.voted_users: set[str] = self._load_set(VOTED_FILE)
        self.words: list[str] = WORDS_FILE.read_text(encoding="utf-8").splitlines(keepends=True)
        self.words_upper: list[str] = WORDS_UPPER_FILE.read_text(encoding="utf-8").splitlines(keepends=True)
        self.reset()

    # --- persistence helpers ---
    @staticmethod
    def _load_set(path: Path) -> set[str]:
        if not path.exists():
            return set()
        return {line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()}

    # --- game lifecycle ---
    def reset(self):
        self.count_play: int = 0
        self.count_own: int = 0
        self.own_word: str | None = None
        self.word_active: bool = False
        self.current_player: int | None = None
        self.rand_num: int = random.randint(1, MAX_WORD_INDEX - 1)
        self.total_votes: int = 0
        self.count: int = 0

    def new_word(self):
        self.rand_num = random.randint(1, MAX_WORD_INDEX - 1)

    def invalidate_word(self):
        """Стирает текущее слово из массивов после угадывания."""
        self.words[self.rand_num] = ""
        self.words_upper[self.rand_num] = ""

    # --- logic ---
    def get_current_word(self) -> str:
        return self.words_upper[self.rand_num].strip()

    def is_correct_guess(self, text: str) -> bool:
        n = self.rand_num
        candidates: set[str] = {
            self.words[n].strip(),
            self.words_upper[n].strip(),
        }
        if self.own_word:
            candidates.add(self.own_word.strip())
        return text.strip() in candidates

    @property
    def is_running(self) -> bool:
        return self.word_active or self.count_play > 0 or self.count_own > 0


state = GameState()

# ---------------------------------------------------------------------------
# Bot & Dispatcher
# ---------------------------------------------------------------------------
bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())


# ---------------------------------------------------------------------------
# Утилиты
# ---------------------------------------------------------------------------
def mention(user_id: int, first_name: str) -> str:
    return f"[{first_name}](tg://user?id={user_id})"


def game_end_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Начать обычную игру", callback_data="host")],
        [InlineKeyboardButton(text="Загадать своё слово", callback_data="own_new")],
    ])


def host_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Посмотреть слово", callback_data="slovo")],
        [InlineKeyboardButton(text="Следующее слово", callback_data="sled")],
    ])


def own_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Просмотреть слово", callback_data="own")],
    ])


def vote_markup(total: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"Проголосовать ({total})", callback_data="vote")],
    ])


def register_user(user) -> None:
    uid = str(user.id)
    if uid not in state.joined_users:
        with USERS_FILE.open("a", encoding="utf-8") as f:
            f.write(uid + "\n")
        state.joined_users.add(uid)
        reg_db(user_id=user.id, answers=0, first_name=user.first_name)


def finalize_round() -> None:
    state.invalidate_word()
    state.reset()
    delete_dup()


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------
@dp.message(Command("start"))
async def cmd_start(message: Message):
    state.chat_id = message.chat.id
    state.reset()
    register_user(message.from_user)
    init_db()
    await message.answer(
        "/play — начать обычную игру\n"
        "/own — загадать своё слово (пишешь боту в ЛС)\n"
        "/stat — статистика\n"
        "/stop — голосование за смену ведущего"
    )


# ---------------------------------------------------------------------------
# /stat
# ---------------------------------------------------------------------------
@dp.message(Command("stat"))
async def cmd_stat(message: Message):
    if not state.joined_users:
        await message.answer("Пока нет игроков.")
        return
    for uid in state.joined_users:
        try:
            info = get_info(user_id=uid)
            await message.answer(
                f"[{info[2]}](tg://user?id={uid}) — {info[1]} ответов",
                parse_mode="Markdown",
            )
        except Exception as e:
            print(f"Stat error for {uid}: {e}")


# ---------------------------------------------------------------------------
# /stop  — голосование за смену ведущего
# ---------------------------------------------------------------------------
@dp.message(Command("stop"))
async def cmd_stop(message: Message):
    state.voted_users.clear()
    VOTED_FILE.write_text("", encoding="utf-8")
    state.total_votes = 0
    await message.answer(
        "Голосование за смену ведущего",
        reply_markup=vote_markup(0),
    )


# ---------------------------------------------------------------------------
# /play
# ---------------------------------------------------------------------------
@dp.message(Command("play"))
async def cmd_play(message: Message):
    if state.is_running:
        await message.answer("Невозможно начать игру — уже идёт.")
        return

    state.current_player = message.from_user.id
    state.word_active = True
    state.count_play += 1

    await message.answer(
        f"{mention(message.from_user.id, message.from_user.first_name)} загадывает слово",
        parse_mode="Markdown",
        reply_markup=host_markup(),
    )


# ---------------------------------------------------------------------------
# /own
# ---------------------------------------------------------------------------
@dp.message(Command("own"))
async def cmd_own(message: Message, fsm_state: FSMContext):
    if state.is_running:
        await message.answer("Невозможно начать другую игру.")
        return

    state.current_player = message.from_user.id
    state.count_own += 1

    if state.chat_id:
        await bot.send_message(
            state.chat_id,
            f"{mention(message.from_user.id, message.from_user.first_name)} вводит слово боту",
            parse_mode="Markdown",
        )

    # Просим слово в ЛС
    await message.answer("Введи слово, которое хочешь загадать:")
    await fsm_state.set_state(WordInput.waiting_for_word)


# ---------------------------------------------------------------------------
# FSM — получение кастомного слова
# ---------------------------------------------------------------------------
@dp.message(WordInput.waiting_for_word)
async def receive_own_word(message: Message, fsm_state: FSMContext):
    word = message.text.strip() if message.text else ""
    if not word:
        await message.answer("Слово не может быть пустым. Попробуй ещё:")
        return

    state.own_word = word
    state.word_active = True
    await fsm_state.clear()

    await message.answer("Слово принято!")

    if state.chat_id:
        await bot.send_message(
            state.chat_id,
            f"{mention(message.from_user.id, message.from_user.first_name)} загадывает слово",
            parse_mode="Markdown",
            reply_markup=own_markup(),
        )


# ---------------------------------------------------------------------------
# Проверка угаданного слова (все текстовые сообщения в чате)
# ---------------------------------------------------------------------------
@dp.message(F.text, F.chat.type.in_({"group", "supergroup"}))
async def check_guess(message: Message):
    # Игра не активна или пишет сам ведущий — пропускаем
    if not state.word_active:
        return
    if message.from_user.id == state.current_player:
        return
    if not state.is_correct_guess(message.text):
        return

    # Правильный ответ!
    uid = message.from_user.id
    await message.answer(
        f"{mention(uid, message.from_user.first_name)} отгадал слово *{message.text.lower()}*",
        parse_mode="Markdown",
        reply_markup=game_end_markup(),
    )

    if check_user(user_id=uid) == (0,):
        reg_db(user_id=uid, answers=1, first_name=message.from_user.first_name)
    else:
        plus_ans(answers=1, user_id=uid)

    finalize_round()


# ---------------------------------------------------------------------------
# Callback-кнопки
# ---------------------------------------------------------------------------
@dp.callback_query(F.data == "slovo")
async def cb_slovo(call: CallbackQuery):
    if call.from_user.id != state.current_player:
        await call.answer("Тебе недоступно это слово.", show_alert=True)
        return
    await call.answer(f"Слово: {state.get_current_word()}", show_alert=True)


@dp.callback_query(F.data == "sled")
async def cb_sled(call: CallbackQuery):
    if call.from_user.id != state.current_player:
        await call.answer("Тебе недоступно это слово.", show_alert=True)
        return
    state.new_word()
    await call.answer(f"Новое слово: {state.get_current_word()}", show_alert=True)
    await call.message.answer(
        f"{mention(call.from_user.id, call.from_user.first_name)} решил заменить слово",
        parse_mode="Markdown",
    )


@dp.callback_query(F.data == "own")
async def cb_own_view(call: CallbackQuery):
    if call.from_user.id != state.current_player or not state.own_word:
        await call.answer("Тебе недоступно это слово.", show_alert=True)
        return
    await call.answer(f"Слово: {state.own_word}", show_alert=True)


@dp.callback_query(F.data == "vote")
async def cb_vote(call: CallbackQuery):
    uid_str = str(call.from_user.id)
    if uid_str in state.voted_users:
        await call.answer("Вы уже проголосовали.", show_alert=True)
        return

    state.voted_users.add(uid_str)
    with VOTED_FILE.open("a", encoding="utf-8") as f:
        f.write(uid_str + "\n")
    state.total_votes += 1
    await call.answer("Ваш голос засчитан!", show_alert=True)

    await call.message.edit_text(
        f"Голосование за смену ведущего. Всего голосов: {state.total_votes}",
        reply_markup=vote_markup(state.total_votes),
    )

    member_count = await bot.get_chat_member_count(call.message.chat.id)
    if state.total_votes >= member_count / 2:
        await call.message.answer("Игра остановлена.")
        finalize_round()


@dp.callback_query(F.data == "host")
async def cb_host(call: CallbackQuery):
    if state.count != 0:
        await call.message.answer("Игра уже идёт.")
        return

    state.count += 1
    state.current_player = call.from_user.id
    state.word_active = True
    state.new_word()

    await call.answer("Теперь ты хост!", show_alert=True)
    await call.message.answer(
        f"{mention(call.from_user.id, call.from_user.first_name)} загадывает слово",
        parse_mode="Markdown",
        reply_markup=host_markup(),
    )


@dp.callback_query(F.data == "own_new")
async def cb_own_new(call: CallbackQuery, fsm_state: FSMContext):
    if state.count != 0:
        await call.message.answer("Игра уже идёт.")
        return

    state.count += 1
    state.current_player = call.from_user.id

    await call.answer("Введи своё слово самому боту.", show_alert=True)
    await bot.send_message(call.from_user.id, "Введи слово, которое хочешь загадать:")
    await fsm_state.set_state(WordInput.waiting_for_word)


# ---------------------------------------------------------------------------
# Запуск
# ---------------------------------------------------------------------------
async def main():
    print("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
