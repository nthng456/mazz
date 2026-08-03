"""
Hydrogram session string generator.

Logs in with a phone number and writes the resulting session string to
telegram_session.txt, which the downloader reads to upload without a
re-login. The session is created in memory, so no .session file is left
behind.

Usage:
    export TG_API_ID=1234567
    export TG_API_HASH=0123456789abcdef0123456789abcdef
    python make_session.py
"""

import asyncio
import os
import sys

from hydrogram import Client
from hydrogram.errors import (
    BadRequest,
    PhoneCodeExpired,
    PhoneCodeInvalid,
    SessionPasswordNeeded,
)

SESSION_FILE = "telegram_session.txt"

# api_id / api_hash come from my.telegram.org. They are per-application, not
# per-account, so they stay in the environment rather than in the file.
API_ID = 36657268
API_HASH = "2ba89ff14685ff1b27643454d1ad49e7"


def prompt(label: str) -> str:
    value = input(label).strip()
    if not value:
        sys.exit("  ✗ لغو شد")
    return value


async def main() -> None:
    if not API_ID or not API_HASH:
        sys.exit(
            "  ✗ TG_API_ID و TG_API_HASH تنظیم نشده‌اند.\n"
            "    از my.telegram.org بگیرید و export کنید:\n"
            "      export TG_API_ID=1234567\n"
            "      export TG_API_HASH=0123456789abcdef0123456789abcdef"
        )

    try:
        api_id = int(API_ID)
    except ValueError:
        sys.exit(f"  ✗ TG_API_ID باید عدد باشد، دریافت شد: {API_ID!r}")

    if os.path.exists(SESSION_FILE):
        answer = input(f"  ⚠  {SESSION_FILE} از قبل وجود دارد. بازنویسی شود؟ [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            sys.exit("  لغو شد — فایل قبلی دست‌نخورده ماند.")

    phone = prompt("  شماره تلفن (با کد کشور، مثل +989121234567): ")

    # in_memory keeps SQLite off disk; the session string is the only artifact.
    app = Client("biomaze_uploader", api_id=api_id, api_hash=API_HASH, in_memory=True)

    await app.connect()
    try:
        sent = await app.send_code(phone)
        print("  → کد تأیید ارسال شد.")

        while True:
            code = prompt("  کد تأیید: ")
            try:
                await app.sign_in(phone, sent.phone_code_hash, code)
                break
            except PhoneCodeInvalid:
                print("  ✗ کد اشتباه است، دوباره تلاش کنید.")
            except PhoneCodeExpired:
                sys.exit("  ✗ کد منقضی شد — اسکریپت را مجدد اجرا کنید.")
            except SessionPasswordNeeded:
                # Two-step verification is on; the cloud password finishes login.
                while True:
                    try:
                        await app.check_password(prompt("  رمز دو مرحله‌ای: "))
                        break
                    except BadRequest:
                        print("  ✗ رمز اشتباه است، دوباره تلاش کنید.")
                break

        session_string = await app.export_session_string()
        me = await app.get_me()
    finally:
        await app.disconnect()

    # The session string is a full account credential: written 0600 so it is
    # not world-readable on shared hosts, and .gitignored so it never commits.
    with open(SESSION_FILE, "w", encoding="utf-8") as f:
        f.write(session_string + "\n")
    try:
        os.chmod(SESSION_FILE, 0o600)
    except OSError:
        pass  # Windows / filesystems without POSIX modes

    print(f"\n  ✓ وارد شدید: {me.first_name} (@{me.username or me.id})")
    print(f"  ✓ سشن ذخیره شد در {SESSION_FILE}")
    print("  ⚠  این فایل دسترسی کامل به اکانت می‌دهد — آن را جایی نفرستید و commit نکنید.")


if __name__ == "__main__":
    asyncio.run(main())
