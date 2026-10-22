
import argparse
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from database import DB_FILE


PROJECT_DIR = Path(__file__).resolve().parent.parent
BACKUP_DIR = PROJECT_DIR / "backups"
TEST_DIR = PROJECT_DIR / "restore_tests"
DB_PATH = Path(DB_FILE).resolve()


def check_database(path):
    path = Path(path).resolve()

    if not path.is_file():
        raise FileNotFoundError(f"Baza topilmadi: {path}")

    connection = sqlite3.connect(
        path.as_uri() + "?mode=ro",
        uri=True,
        timeout=5
    )

    try:
        result = connection.execute(
            "PRAGMA integrity_check"
        ).fetchone()[0]

        if result != "ok":
            raise RuntimeError(
                f"Database integrity xatosi: {result}"
            )

        tables = connection.execute("""
            SELECT name
            FROM sqlite_master
            WHERE type = 'table'
        """).fetchall()

        if not tables:
            raise RuntimeError("Bazadagi jadvallar topilmadi.")

        return len(tables)
    finally:
        connection.close()


def latest_backup():
    backups = sorted(
        BACKUP_DIR.glob("sagban_*.db"),
        key=lambda p: p.stat().st_mtime,
        reverse=True
    )

    if not backups:
        raise FileNotFoundError("Backup topilmadi.")

    return backups[0].resolve()


def copy_database(source, destination):
    source = Path(source).resolve()
    destination = Path(destination).resolve()

    if source == destination:
        raise RuntimeError("Manba va manzil bir xil!")

    destination.parent.mkdir(parents=True, exist_ok=True)

    if destination.exists():
        raise FileExistsError(
            f"Manzil fayli allaqachon mavjud: {destination}"
        )

    src = sqlite3.connect(
        source.as_uri() + "?mode=ro",
        uri=True,
        timeout=10
    )
    dst = sqlite3.connect(str(destination), timeout=10)

    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()

    return check_database(destination)


def restore_test(source):
    TEST_DIR.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    target = TEST_DIR / f"restore_test_{stamp}.db"

    tables = copy_database(source, target)

    print("\nSAGBAN CRM - RESTORE TEST")
    print("Backup:", source.name)
    print("Test bazasi:", target)
    print("Integrity: ok")
    print("Jadvallar:", tables)
    print("Asosiy baza o'zgartirilmadi.")
    print("RESTORE TEST MUVAFFAQIYATLI")


def restore_production(source):
    print("\nSAGBAN CRM - PRODUCTION RESTORE")
    print("Manba:", source)
    print("Asosiy baza:", DB_PATH)

    print(
        "\nDIQQAT: Telegram bot, Web Admin va boshqa "
        "baza foydalanuvchilari to'xtatilgan bo'lishi shart."
    )

    confirmation = input(
        "\nDavom etish uchun RESTORE deb yozing: "
    ).strip()

    if confirmation != "RESTORE":
        print("Bekor qilindi.")
        return

    check_database(source)

    if not DB_PATH.exists():
        raise RuntimeError(
            "Asosiy baza topilmadi. Avtomatik almashtirish bekor qilindi."
        )

    connection = sqlite3.connect(DB_PATH, timeout=3)

    try:
        checkpoint = connection.execute(
            "PRAGMA wal_checkpoint(TRUNCATE)"
        ).fetchone()

        if checkpoint[0] != 0:
            raise RuntimeError(
                f"WAL checkpoint bajarilmadi: {checkpoint}"
            )

        integrity = connection.execute(
            "PRAGMA integrity_check"
        ).fetchone()[0]

        if integrity != "ok":
            raise RuntimeError(
                f"Asosiy baza integrity xatosi: {integrity}"
            )
    finally:
        connection.close()

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")

    emergency_backup = (
        BACKUP_DIR / f"before_restore_{stamp}.db"
    )

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    copy_database(DB_PATH, emergency_backup)

    print("Favqulodda backup:", emergency_backup)

    wal = Path(str(DB_PATH) + "-wal")
    shm = Path(str(DB_PATH) + "-shm")

    if wal.exists() or shm.exists():
        raise RuntimeError(
            "WAL/SHM fayllari mavjud. "
            "Production restore xavfsizlik sababli to'xtatildi. "
            "Ularni qo'lda o'chirmang."
        )

    temporary = DB_PATH.with_name(
        DB_PATH.name + f".restore_{stamp}.tmp"
    )

    copy_database(source, temporary)

    # Fayl almashtirilishidan oldingi so'nggi tasdiq
    confirmation = input(
        "\nAsosiy bazani almashtirish uchun YES deb yozing: "
    ).strip()

    if confirmation != "YES":
        temporary.unlink(missing_ok=True)
        print("Almashtirish bekor qilindi.")
        return

    os.replace(temporary, DB_PATH)

    tables = check_database(DB_PATH)

    print("\nPRODUCTION RESTORE MUVAFFAQIYATLI")
    print("Integrity: ok")
    print("Jadvallar:", tables)
    print("Favqulodda backup:", emergency_backup)


def main():
    parser = argparse.ArgumentParser(
        description="SAGBAN CRM Restore"
    )

    parser.add_argument(
        "--backup",
        help="Backup faylining manzili"
    )

    parser.add_argument(
        "--production",
        action="store_true",
        help="Asosiy bazani tiklash"
    )

    args = parser.parse_args()

    source = (
        Path(args.backup).resolve()
        if args.backup
        else latest_backup()
    )

    if not source.is_file():
        raise FileNotFoundError(
            f"Backup topilmadi: {source}"
        )

    if source == DB_PATH:
        raise RuntimeError(
            "Asosiy bazani backup sifatida tanlash mumkin emas."
        )

    if args.production:
        restore_production(source)
    else:
        restore_test(source)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"\nXATOLIK: {exc}", file=sys.stderr)
        sys.exit(1)
