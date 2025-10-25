#!/usr/bin/env python3
"""
Whitenet Telegram bot (text posts, likes, follows).
Usage:
  - Set env var TELEGRAM_BOT_TOKEN
  - (Optional) Set DATABASE_URL (Postgres) otherwise sqlite file whitenet.db will be used.
Run:
  python bot.py
"""
import os
import logging
from datetime import datetime

from aiogram import Bot, Dispatcher, types
from aiogram.utils import executor
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from sqlalchemy import (
    Column, Integer, String, Text, DateTime, ForeignKey, create_engine, func, UniqueConstraint
)
from sqlalchemy.orm import sessionmaker, relationship, declarative_base
from sqlalchemy.exc import IntegrityError

from dotenv import load_dotenv
load_dotenv()

# ---------- Config ----------
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
if not TELEGRAM_TOKEN:
    raise RuntimeError("Set TELEGRAM_BOT_TOKEN env variable")

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///whitenet.db")
# ---------------------------

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# SQLAlchemy setup
connect_args = {}
if DATABASE_URL.startswith("sqlite"):
    connect_args = {"check_same_thread": False}

engine = create_engine(DATABASE_URL, echo=False, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()


# ---------- Models ----------
class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)  # Telegram user_id
    username = Column(String(64), nullable=True)
    display_name = Column(String(200), nullable=True)
    bio = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    posts = relationship("Post", back_populates="author")
    likes = relationship("Like", back_populates="user")


class Post(Base):
    __tablename__ = "posts"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"))
    text = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    likes_count = Column(Integer, default=0)

    author = relationship("User", back_populates="posts")
    likes = relationship("Like", back_populates="post")


class Like(Base):
    __tablename__ = "likes"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"))
    post_id = Column(Integer, ForeignKey("posts.id", ondelete="CASCADE"))
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="likes")
    post = relationship("Post", back_populates="likes")
    __table_args__ = (UniqueConstraint("user_id", "post_id", name="_user_post_uc"),)


class Subscription(Base):
    __tablename__ = "subscriptions"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, nullable=False)   # who follows
    target_id = Column(Integer, nullable=False) # who is followed
    created_at = Column(DateTime, default=datetime.utcnow)
    __table_args__ = (UniqueConstraint("user_id", "target_id", name="_user_target_uc"),)


# Create tables
Base.metadata.create_all(bind=engine)


# ---------- Helpers ----------
def get_or_create_user(session, tg_user: types.User):
    user = session.query(User).get(tg_user.id)
    if not user:
        user = User(
            id=tg_user.id,
            username=tg_user.username,
            display_name=(tg_user.full_name if hasattr(tg_user, "full_name") else tg_user.username)
        )
        session.add(user)
        try:
            session.commit()
        except Exception:
            session.rollback()
            user = session.query(User).get(tg_user.id)
    else:
        # update username/display name if changed
        updated = False
        if user.username != tg_user.username:
            user.username = tg_user.username
            updated = True
        full_name = tg_user.full_name if hasattr(tg_user, "full_name") else tg_user.username
        if user.display_name != full_name:
            user.display_name = full_name
            updated = True
        if updated:
            session.add(user)
            session.commit()
    return user


def build_post_keyboard(post_id: int, author_id: int, session):
    kb = InlineKeyboardMarkup(row_width=3)
    # Like button shows current count
    post = session.query(Post).get(post_id)
    likes = post.likes_count if post else 0
    kb.insert(InlineKeyboardButton(text=f"❤️ {likes}", callback_data=f"like:{post_id}"))
    kb.insert(InlineKeyboardButton(text="Профиль автора", callback_data=f"profile:{author_id}"))
    kb.insert(InlineKeyboardButton(text="Подписаться", callback_data=f"follow:{author_id}"))
    return kb


# ---------- Bot setup ----------
bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher(bot)


# ---------- Handlers ----------
@dp.message_handler(commands=["start"])
async def cmd_start(message: types.Message):
    session = SessionLocal()
    user = get_or_create_user(session, message.from_user)
    await message.answer(
        "Привет! Это Whitenet — простой текстовый социальный бот.\n\n"
        "Команды:\n"
        "/post <текст> — создать пост\n"
        "/feed — лента (последние посты)\n"
        "/my_posts — мои посты\n"
        "/profile — показать профиль\n"
        "/setbio <текст> — установить bio\n"
        "/follow <user_id> — подписаться (по id)\n"
        "/unfollow <user_id> — отписаться\n"
        "/help — помощь"
    )
    session.close()


@dp.message_handler(commands=["help"])
async def cmd_help(message: types.Message):
    await cmd_start(message)


@dp.message_handler(commands=["setbio"])
async def cmd_setbio(message: types.Message):
    text = message.get_args()
    if not text:
        await message.reply("Использование: /setbio Текст вашего bio")
        return
    session = SessionLocal()
    user = get_or_create_user(session, message.from_user)
    user.bio = text[:1000]
    session.add(user)
    session.commit()
    await message.reply("Bio обновлён.")
    session.close()


@dp.message_handler(commands=["profile"])
async def cmd_profile(message: types.Message):
    session = SessionLocal()
    # If user specified id: /profile <id>
    args = message.get_args().strip()
    if args:
        try:
            uid = int(args)
        except:
            await message.reply("Неправильный id пользователя.")
            session.close()
            return
        user = session.query(User).get(uid)
        if not user:
            await message.reply("Пользователь не найден.")
            session.close()
            return
    else:
        user = get_or_create_user(session, message.from_user)

    posts_count = session.query(func.count(Post.id)).filter(Post.user_id == user.id).scalar()
    followers = session.query(func.count(Subscription.id)).filter(Subscription.target_id == user.id).scalar()
    following = session.query(func.count(Subscription.id)).filter(Subscription.user_id == user.id).scalar()

    text = (
        f"👤 {user.display_name} (@{user.username})\n"
        f"📝 Постов: {posts_count}\n"
        f"👥 Подписчики: {followers}\n"
        f"➡️ Подписки: {following}\n"
        f"💬 Bio: {user.bio or '—'}\n"
        f"ID: {user.id}"
    )
    await message.reply(text)
    session.close()


@dp.message_handler(commands=["post"])
async def cmd_post(message: types.Message):
    text = message.get_args().strip()
    if not text:
        await message.reply("Использование: /post ТЕКСТ. Пример: /post Привет, это мой первый пост!")
        return
    session = SessionLocal()
    user = get_or_create_user(session, message.from_user)
    post = Post(user_id=user.id, text=text[:2000])
    session.add(post)
    session.commit()
    await message.reply("Пост опубликован!", reply_markup=build_post_keyboard(post.id, user.id, session))
    session.close()


@dp.message_handler(commands=["my_posts"])
async def cmd_my_posts(message: types.Message):
    session = SessionLocal()
    user = get_or_create_user(session, message.from_user)
    posts = session.query(Post).filter(Post.user_id == user.id).order_by(Post.created_at.desc()).limit(10).all()
    if not posts:
        await message.reply("У вас ещё нет постов. Создайте первым: /post ТЕКСТ")
        session.close()
        return
    for p in posts:
        kb = build_post_keyboard(p.id, p.user_id, session)
        created = p.created_at.strftime("%Y-%m-%d %H:%M")
        await message.answer(f"{p.text}\n\n🕒 {created}", reply_markup=kb)
    session.close()


@dp.message_handler(commands=["feed"])
async def cmd_feed(message: types.Message):
    session = SessionLocal()
    # personalized feed: posts from people user follows; if none -> recent global
    user = get_or_create_user(session, message.from_user)
    follows = session.query(Subscription.target_id).filter(Subscription.user_id == user.id).all()
    follow_ids = [f[0] for f in follows]
    if follow_ids:
        posts = session.query(Post).filter(Post.user_id.in_(follow_ids)).order_by(Post.created_at.desc()).limit(20).all()
    else:
        posts = session.query(Post).order_by(Post.created_at.desc()).limit(20).all()

    if not posts:
        await message.reply("Лента пуста — пока нет постов. Попросите друзей написать /post")
        session.close()
        return

    for p in posts:
        author = session.query(User).get(p.user_id)
        kb = build_post_keyboard(p.id, p.user_id, session)
        created = p.created_at.strftime("%Y-%m-%d %H:%M")
        head = f"👤 {author.display_name} (@{author.username})\n"
        await message.answer(f"{head}{p.text}\n\n🕒 {created}", reply_markup=kb)
    session.close()


@dp.message_handler(commands=["follow"])
async def cmd_follow(message: types.Message):
    args = message.get_args().strip()
    if not args:
        await message.reply("Использование: /follow <user_id>")
        return
    try:
        target_id = int(args)
    except:
        await message.reply("Неверный user_id.")
        return
    session = SessionLocal()
    user = get_or_create_user(session, message.from_user)
    if user.id == target_id:
        await message.reply("Нельзя подписаться на самого себя.")
        session.close()
        return
    # check target exists
    target = session.query(User).get(target_id)
    if not target:
        await message.reply("Пользователь с таким id не найден.")
        session.close()
        return
    sub = Subscription(user_id=user.id, target_id=target_id)
    session.add(sub)
    try:
        session.commit()
        await message.reply(f"Вы подписаны на {target.display_name}.")
    except IntegrityError:
        session.rollback()
        await message.reply("Вы уже подписаны.")
    session.close()


@dp.message_handler(commands=["unfollow"])
async def cmd_unfollow(message: types.Message):
    args = message.get_args().strip()
    if not args:
        await message.reply("Использование: /unfollow <user_id>")
        return
    try:
        target_id = int(args)
    except:
        await message.reply("Неверный user_id.")
        return
    session = SessionLocal()
    user = get_or_create_user(session, message.from_user)
    deleted = session.query(Subscription).filter(Subscription.user_id == user.id, Subscription.target_id == target_id).delete()
    session.commit()
    if deleted:
        await message.reply("Отписались.")
    else:
        await message.reply("Вы не были подписаны.")
    session.close()


# Callback handler for inline buttons (like/profile/follow)
@dp.callback_query_handler(lambda c: c.data)
async def process_callback(callback_query: types.CallbackQuery):
    data = callback_query.data
    session = SessionLocal()
    try:
        if data.startswith("like:"):
            post_id = int(data.split(":", 1)[1])
            user = get_or_create_user(session, callback_query.from_user)
            # attempt to create like
            like = Like(user_id=user.id, post_id=post_id)
            session.add(like)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                await callback_query.answer("Вы уже ставили лайк этому посту.", show_alert=False)
                session.close()
                return
            # increment counter
            post = session.query(Post).get(post_id)
            if post:
                post.likes_count = (post.likes_count or 0) + 1
                session.add(post)
                session.commit()
            await callback_query.answer("Понравилось! ❤️", show_alert=False)
            # edit message keyboard to update like count (best-effort)
            try:
                await bot.edit_message_reply_markup(
                    chat_id=callback_query.message.chat.id,
                    message_id=callback_query.message.message_id,
                    reply_markup=build_post_keyboard(post_id, post.user_id, session)
                )
            except Exception:
                pass

        elif data.startswith("profile:"):
            target_id = int(data.split(":", 1)[1])
            target = session.query(User).get(target_id)
            if not target:
                await callback_query.answer("Пользователь не найден.", show_alert=True)
            else:
                posts_count = session.query(func.count(Post.id)).filter(Post.user_id == target.id).scalar()
                followers = session.query(func.count(Subscription.id)).filter(Subscription.target_id == target.id).scalar()
                following = session.query(func.count(Subscription.id)).filter(Subscription.user_id == target.id).scalar()
                text = (
                    f"👤 {target.display_name} (@{target.username})\n"
                    f"📝 Постов: {posts_count}\n"
                    f"👥 Подписчики: {followers}\n"
                    f"➡️ Подписки: {following}\n"
                    f"💬 Bio: {target.bio or '—'}\n"
                    f"ID: {target.id}"
                )
                await bot.send_message(callback_query.from_user.id, text)
                await callback_query.answer()
        elif data.startswith("follow:"):
            target_id = int(data.split(":", 1)[1])
            user = get_or_create_user(session, callback_query.from_user)
            if user.id == target_id:
                await callback_query.answer("Нельзя подписаться на себя.", show_alert=True)
                session.close()
                return
            sub = Subscription(user_id=user.id, target_id=target_id)
            session.add(sub)
            try:
                session.commit()
                await callback_query.answer("Подписка оформлена.")
            except IntegrityError:
                session.rollback()
                await callback_query.answer("Вы уже подписаны.")
        else:
            await callback_query.answer()
    finally:
        session.close()


@dp.message_handler()
async def fallback(message: types.Message):
    # Friendly fallback: if user sends text without /post maybe it's intended as a post?
    # We do nothing automatically; show quick help
    await message.reply("Неизвестная команда. Используйте /help. Чтобы создать пост — /post ТЕКСТ")


# ---------- Entry point ----------
if __name__ == "__main__":
    logger.info("Starting Whitenet bot (polling mode)")
    # Start long polling
    executor.start_polling(dp, skip_updates=True)
