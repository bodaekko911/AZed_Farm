import asyncio
from datetime import date
from sqlalchemy import select, and_, func
from sqlalchemy.orm import selectinload
from app.database import AsyncSessionLocal
from app.models.carbon import CarbonEmissionFactor, CarbonLog, CarbonTarget
from app.models.user import User

async def main():
    try:
        async with AsyncSessionLocal() as db:
            d_from = date(2025, 1, 1)
            d_to = date(2026, 12, 31)

            print("Fetching totals by source_type...")
            rows = await db.execute(
                select(
                    CarbonEmissionFactor.source_type,
                    func.sum(CarbonLog.kg_co2e).label("total"),
                )
                .join(CarbonEmissionFactor, CarbonLog.factor_id == CarbonEmissionFactor.id)
                .where(CarbonLog.log_date.between(d_from, d_to))
                .group_by(CarbonEmissionFactor.source_type)
            )
            by_type = {r.source_type: float(r.total or 0) for r in rows}
            print(by_type)

            print("Fetching latest logs...")
            logs_q = await db.execute(
                select(CarbonLog)
                .options(
                    selectinload(CarbonLog.factor),
                    selectinload(CarbonLog.farm),
                    selectinload(CarbonLog.user),
                )
                .where(CarbonLog.log_date.between(d_from, d_to))
                .order_by(CarbonLog.log_date.desc(), CarbonLog.id.desc())
                .limit(50)
            )
            logs = logs_q.scalars().all()
            print("Logs count:", len(logs))

            print("Fetching targets...")
            targets_q = await db.execute(
                select(CarbonTarget)
                .where(
                    and_(
                        CarbonTarget.period_start <= d_to,
                        CarbonTarget.period_end   >= d_from,
                    )
                )
                .order_by(CarbonTarget.period_start)
            )
            targets = targets_q.scalars().all()
            print("Targets count:", len(targets))

    except Exception as e:
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(main())
