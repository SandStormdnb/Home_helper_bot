from sqlalchemy import create_engine, Column, Integer, String, Boolean, DateTime, Date, ForeignKey
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
from datetime import datetime, date

DATABASE_URL = "sqlite:///tasks.db"
Base = declarative_base()

class Category(Base):
    __tablename__ = "categories"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False)   # Telegram ID
    name = Column(String, nullable=False)       # Название категории

    tasks = relationship("Task", back_populates="category")

class Task(Base):
    __tablename__ = "tasks"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, nullable=False)
    title = Column(String, nullable=False)
    
    # Категория (связь)
    category_id = Column(Integer, ForeignKey('categories.id'), nullable=True)
    category = relationship("Category", back_populates="tasks")
    
    due_time = Column(String, nullable=False)        # формат "HH:MM"
    start_date = Column(Date, default=date.today)    # с какого дня начинать
    
    # Тип повтора:
    # 'none', 'daily', 'weekly', 'interval'
    repeat_type = Column(String, default='none')
    repeat_days = Column(String, nullable=True)      # для weekly: "mon,tue"
    interval_days = Column(Integer, default=0)       # для interval: количество дней
    
    # Дополнительное напоминание (за N минут)
    reminder_offset = Column(Integer, default=0)     # 0 - без доп.напоминания
    
    is_done = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.now)

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(bind=engine)
Base.metadata.create_all(bind=engine)