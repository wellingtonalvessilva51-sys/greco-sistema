from sqlalchemy import create_engine, Column, Integer, String, Float, Boolean, DateTime, ForeignKey, Text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
from datetime import datetime
import os

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./greco.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

class Loja(Base):
    __tablename__ = "lojas"
    id = Column(Integer, primary_key=True)
    nome = Column(String, nullable=False)
    ativa = Column(Boolean, default=True)
    meta_mensal = Column(Float, default=0)
    vendedoras = relationship("Vendedora", back_populates="loja")

class Vendedora(Base):
    __tablename__ = "vendedoras"
    id = Column(Integer, primary_key=True)
    nome = Column(String, nullable=False)
    email = Column(String, unique=True, nullable=False)
    senha_hash = Column(String, nullable=False)
    loja_id = Column(Integer, ForeignKey("lojas.id"), nullable=True)
    bling_vendedor_nome = Column(String, default="")
    is_gerente = Column(Boolean, default=False)
    ativa = Column(Boolean, default=True)
    meta_mensal = Column(Float, default=0)
    percentual_comissao = Column(Float, default=5.0)
    loja = relationship("Loja", back_populates="vendedoras")

class Venda(Base):
    __tablename__ = "vendas"
    id = Column(Integer, primary_key=True)
    bling_pedido_id = Column(String, unique=True, nullable=False)
    vendedora_nome = Column(String, default="")
    loja_id = Column(Integer, ForeignKey("lojas.id"), nullable=True)
    cliente_nome = Column(String, default="")
    valor_total = Column(Float, default=0)
    num_itens = Column(Integer, default=0)
    data_venda = Column(DateTime, default=datetime.utcnow)
    situacao = Column(String, default="")
    sincronizado_em = Column(DateTime, default=datetime.utcnow)

class TokenBling(Base):
    __tablename__ = "tokens_bling"
    id = Column(Integer, primary_key=True)
    loja_id = Column(Integer, ForeignKey("lojas.id"), nullable=True)
    access_token = Column(Text)
    refresh_token = Column(Text)
    expires_at = Column(DateTime)
    atualizado_em = Column(DateTime, default=datetime.utcnow)

def criar_tabelas():
    Base.metadata.create_all(bind=engine)
