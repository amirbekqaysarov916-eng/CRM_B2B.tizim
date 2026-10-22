
from __future__ import annotations

import argparse
import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from database import DB_FILE


BASE_DIR = Path(__file__).resolve().parent.parent
BACKUP_DIR = BASE_DIR / "backups"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

log = logging.getLogger("sagban.backup")


def create_backup(retention_days: int = 30) -> Path:
    if retention_days < 1:
        raise ValueError("retention_days kamida 1 bo'lishi kerak")

    source = Path(DB_FILE).resolve()

    if not source.is_file():
        raise FileNotFoundError(f"Baza topilmadi: {source}")

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")

    destination = BACKUP_DIR / f"sagban_{timestamp}.db"
    temporary = BACKUP_DIR / f".sagban_{timestamp}.tmp"

    src = None
    dst = None
    check = None

    try:
        # 1. SQLite bazasini ochamiz
        src = sqlite3.connect(
            source.as_uri() + "?mode=ro",
            uri=True,
            timeout=30,
        )

        dst = sqlite3.connect(
            str(temporary),
            timeout=30,
        )

        # 2. Izchil backup yaratamiz
        src.backup(
            dst,
            pages=100,
            sleep=0.25,
        )

        # 3. Fayllarni to'liq yopamiz
        dst.close()
        dst = None

        src.close()
        src = None

        # 4. Backup yaxlitligini tekshiramiz
        check = sqlite3.connect(
            temporary.as_uri() + "?mode=ro",
            uri=True,
        )

        result = check.execute(
            "PRAGMA integrity_check"
        ).fetchone()

        if not result or result[0] != "ok":
            raise RuntimeError(
                f"Backup yaxlitligi buzilgan: {result}"
            )

        check.close()
        check = None

        # 5. Tekshirilgan backupni saqlaymiz
        temporary.replace(destination)

        log.info("BACKUP MUVAFFAQIYATLI: %s", destination)

        log.info(
            "Hajmi: %.2f MB",
            destination.stat().st_size / (1024 * 1024),
        )

        # 6. Eski backuplarni tozalaymiz
        cutoff = datetime.now() - timedelta(
            days=retention_days
        )

        for old in BACKUP_DIR.glob("sagban_*.db"):
            if old == destination:
                continue

            modified = datetime.fromtimestamp(
                old.stat().st_mtime
            )

            if modified < cutoff:
                old.unlink()
                log.info(
                    "Eski backup o'chirildi: %s",
                    old.name,
                )

        return destination

    finally:
        # Har qanday xatoda ulanishlarni yopamiz
        for connection in (check, dst, src):
            if connection is not None:
                connection.close()

        if temporary.exists():
            try:
                temporary.unlink()
            except PermissionError:
                log.warning(
                    "Vaqtinchalik faylni o'chirib bo'lmadi: %s",
                    temporary,
                )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--retention-days",
        type=int,
        default=30,
    )

    args = parser.parse_args()

    try:
        create_backup(args.retention_days)

    except Exception:
        log.exception("Backup yaratishda xatolik")
        raise SystemExit(1)
