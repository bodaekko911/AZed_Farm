import asyncio
from app.app_factory import create_app
from app.database import get_async_session
from sqlalchemy import select
from app.models.accounting import Journal

async def check():
    async for db in get_async_session():
        res = await db.execute(select(Journal).limit(1))
        j = res.scalar_one_or_none()
        if j:
            print("created_at:", type(j.created_at), j.created_at)
            # Try comparison with d_from
            from datetime import datetime, timezone
            d_from = datetime.fromisoformat("2020-01-01").replace(tzinfo=timezone.utc)
            try:
                print("Comparison:", d_from <= j.created_at)
            except Exception as e:
                print("Error:", e)
        else:
            print("No journals found.")
        break

if __name__ == "__main__":
    asyncio.run(check())
