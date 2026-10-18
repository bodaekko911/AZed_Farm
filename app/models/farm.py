from sqlalchemy import Column, Integer, String, Numeric, DateTime, ForeignKey, Text, Date
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.database import Base


class Farm(Base):
    __tablename__ = "farms"

    id         = Column(Integer, primary_key=True, index=True)
    name       = Column(String(150), nullable=False, unique=True)
    location   = Column(String(200))
    notes      = Column(Text)
    is_active  = Column(Integer, default=1)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    deliveries   = relationship("FarmDelivery", back_populates="farm")
    weather_logs = relationship("WeatherLog", back_populates="farm")
    employees    = relationship("Employee", back_populates="farm")
    plots        = relationship("FarmPlot", back_populates="farm")


class FarmDelivery(Base):
    __tablename__ = "farm_deliveries"

    id             = Column(Integer, primary_key=True, index=True)
    delivery_number= Column(String(30), unique=True, index=True)
    farm_id        = Column(Integer, ForeignKey("farms.id"), nullable=False)
    user_id        = Column(Integer, ForeignKey("users.id"), nullable=True)
    delivery_date  = Column(Date, nullable=False)
    received_by    = Column(String(150))
    quality_notes  = Column(Text)
    notes          = Column(Text)
    # ── Transport (carbon footprint) ──
    distance_km    = Column(Numeric(8, 1), nullable=True)   # one-way km farm → site
    vehicle_type   = Column(String(20), nullable=True)       # 'van' | 'truck' → carbon factor
    created_at     = Column(DateTime(timezone=True), server_default=func.now())

    farm  = relationship("Farm", back_populates="deliveries")
    user  = relationship("User")
    items = relationship("FarmDeliveryItem", back_populates="delivery",
                         cascade="all, delete-orphan")


class FarmDeliveryItem(Base):
    __tablename__ = "farm_delivery_items"

    id          = Column(Integer, primary_key=True, index=True)
    delivery_id = Column(Integer, ForeignKey("farm_deliveries.id"), nullable=False)
    product_id  = Column(Integer, ForeignKey("products.id"), nullable=False)
    qty         = Column(Numeric(12, 3), nullable=False)
    unit        = Column(String(30))
    notes       = Column(String(255))
    # The greenhouse / field this was harvested from (optional) — drives plot production totals.
    plot_id     = Column(Integer, ForeignKey("farm_plots.id"), nullable=True, index=True)

    delivery = relationship("FarmDelivery", back_populates="items")
    product  = relationship("Product")
    plot     = relationship("FarmPlot")


class WeatherLog(Base):
    __tablename__ = "weather_logs"

    id           = Column(Integer, primary_key=True, index=True)
    farm_id      = Column(Integer, ForeignKey("farms.id"), nullable=False)
    log_date     = Column(Date, nullable=False)
    temp_min     = Column(Numeric(5, 1), nullable=True)   # °C
    temp_max     = Column(Numeric(5, 1), nullable=True)   # °C
    rainfall_mm  = Column(Numeric(7, 2), nullable=True)
    humidity_pct = Column(Numeric(5, 1), nullable=True)   # %
    notes        = Column(Text, nullable=True)
    created_at   = Column(DateTime(timezone=True), server_default=func.now())

    farm = relationship("Farm", back_populates="weather_logs")


PLOT_TYPES = ("greenhouse", "field")
PLOT_INPUT_TYPES = ("seed", "fertilizer", "pesticide", "other")


class FarmPlot(Base):
    """A greenhouse or open field on a farm.

    Seeds and fertilizers applied to it are logged as PlotInput rows; what it
    produced comes from farm delivery items tagged with its id.
    """
    __tablename__ = "farm_plots"

    id         = Column(Integer, primary_key=True, index=True)
    farm_id    = Column(Integer, ForeignKey("farms.id"), nullable=False, index=True)
    name       = Column(String(150), nullable=False)
    plot_type  = Column(String(20), nullable=False, default="greenhouse")   # greenhouse | field
    area_m2    = Column(Numeric(12, 2), nullable=True)
    notes      = Column(Text, nullable=True)
    is_active  = Column(Integer, default=1)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    farm   = relationship("Farm", back_populates="plots")
    inputs = relationship("PlotInput", back_populates="plot")


class PlotInput(Base):
    """Seeds, fertilizer or another input applied to a greenhouse / field.

    Either taken from inventory (product_id + location_id set: stock is
    deducted at that storage, as with animal feeding) or typed in by name
    with its cost when it was bought straight for the plot.
    """
    __tablename__ = "plot_inputs"

    id          = Column(Integer, primary_key=True, index=True)
    plot_id     = Column(Integer, ForeignKey("farm_plots.id"), nullable=False, index=True)
    input_type  = Column(String(20), nullable=False, default="seed")   # seed | fertilizer | pesticide | other
    product_id  = Column(Integer, ForeignKey("products.id"), nullable=True, index=True)
    location_id = Column(Integer, ForeignKey("stock_locations.id"), nullable=True)
    item_name   = Column(String(200), nullable=False)
    qty         = Column(Numeric(14, 4), nullable=False)
    unit        = Column(String(30), nullable=True)
    total_cost  = Column(Numeric(14, 2), nullable=True)
    crop        = Column(String(150), nullable=True)   # what the seed is for / the crop being fed
    applied_on  = Column(Date, nullable=False, index=True)
    note        = Column(Text, nullable=True)
    user_id     = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at  = Column(DateTime(timezone=True), server_default=func.now())

    plot     = relationship("FarmPlot", back_populates="inputs")
    product  = relationship("Product")
    location = relationship("StockLocation")
    user     = relationship("User")


FISH_LOG_TYPES = ("feed", "stock_in", "death", "harvest")


class FishTank(Base):
    """A fish tank in a sandponic bed (SPC): the fish's waste feeds the bed's crops, so their feed is the bed's input.
    Kept apart from the Animals module on purpose — fish here are part of the growing system."""
    __tablename__ = "fish_tanks"

    id         = Column(Integer, primary_key=True, index=True)
    plot_id    = Column(Integer, ForeignKey("farm_plots.id"), nullable=False, index=True)
    name       = Column(String(150), nullable=False)
    species    = Column(String(100), nullable=True)
    fish_count = Column(Integer, nullable=False, default=0)    # moved by stock_in / death / harvest logs
    notes      = Column(Text, nullable=True)
    is_active  = Column(Integer, default=1)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    plot = relationship("FarmPlot")
    logs = relationship("FishLog", back_populates="tank")


class FishLog(Base):
    """One event in a fish tank.

    feed     — feed from stock (product + storage; stock goes down), valued at the product's cost then
    stock_in — fish added (count up)
    death    — fish died (count down)
    harvest  — fish taken out (count down), with their weight
    """
    __tablename__ = "fish_logs"

    id          = Column(Integer, primary_key=True, index=True)
    tank_id     = Column(Integer, ForeignKey("fish_tanks.id"), nullable=False, index=True)
    log_type    = Column(String(20), nullable=False)
    log_date    = Column(Date, nullable=False, index=True)
    product_id  = Column(Integer, ForeignKey("products.id"), nullable=True, index=True)
    location_id = Column(Integer, ForeignKey("stock_locations.id"), nullable=True)
    qty         = Column(Numeric(14, 4), nullable=True)      # feed quantity, in the product's unit
    total_cost  = Column(Numeric(14, 2), nullable=True)      # feed value at the time
    fish_count  = Column(Integer, nullable=True)             # fish added / dead / harvested
    weight_kg   = Column(Numeric(12, 3), nullable=True)      # harvest weight
    note        = Column(Text, nullable=True)
    user_id     = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at  = Column(DateTime(timezone=True), server_default=func.now())

    tank     = relationship("FishTank", back_populates="logs")
    product  = relationship("Product")
    location = relationship("StockLocation")
    user     = relationship("User")
