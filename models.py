from sqlalchemy import create_engine, Column, Integer, String, Float, Boolean, DateTime, ForeignKey, Text, text, BigInteger
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

class Produto(Base):
    __tablename__ = "produtos"
    id = Column(Integer, primary_key=True)
    bling_produto_id = Column(String, unique=True, nullable=False)
    nome = Column(String, nullable=False)
    sku = Column(String, nullable=False)
    gtin = Column(String, default="")
    ncm = Column(String, default="")
    preco_venda = Column(Float, default=0)
    preco_custo = Column(Float, default=0)
    estoque_inicial = Column(Integer, default=0)
    descricao = Column(Text, default="")
    tem_variacoes = Column(Boolean, default=False)
    imagem_url = Column(String, default="")
    criado_em = Column(DateTime, default=datetime.utcnow)
    imagens_modelos = relationship("ModeloImagem", back_populates="produto", cascade="all, delete-orphan")

class ModeloImagem(Base):
    __tablename__ = "modelos_imagens"
    id = Column(Integer, primary_key=True)
    produto_id = Column(Integer, ForeignKey("produtos.id"), nullable=False)
    tamanho = Column(String, default="")
    tipo_modelo = Column(String, default="")
    imagem_url = Column(String, default="")
    prompt_usado = Column(Text, default="")
    criado_em = Column(DateTime, default=datetime.utcnow)
    produto = relationship("Produto", back_populates="imagens_modelos")

class PedidoVendedorCache(Base):
    __tablename__ = "pedido_vendedor_cache"
    pedido_id  = Column(BigInteger, primary_key=True)
    vendor_id  = Column(BigInteger, nullable=True)   # None = pedido sem vendedor
    total_itens = Column(Float, nullable=True)        # soma das quantidades dos itens
    cached_at  = Column(DateTime, default=datetime.utcnow)

class ProdutoBlingCache(Base):
    """Catálogo completo do Bling (todos os produtos, não só os cadastrados
    pelo Modexa) + saldo de estoque — sincronizado em background, porque
    buscar tudo ao vivo (6000+ produtos) demora mais de 1 minuto."""
    __tablename__ = "produtos_bling_cache"
    id = Column(BigInteger, primary_key=True)  # id do produto no Bling
    nome = Column(String, default="")
    sku = Column(String, default="")
    preco = Column(Float, default=0)
    tipo = Column(String, default="")
    situacao = Column(String, default="")
    imagem_url = Column(String, default="")
    estoque_atual = Column(Float, nullable=True)
    atualizado_em = Column(DateTime, default=datetime.utcnow)

def criar_tabelas():
    Base.metadata.create_all(bind=engine)
    try:
        with engine.connect() as conn:
            conn.execute(text("ALTER TABLE produtos ADD COLUMN imagem_url TEXT DEFAULT ''"))
            conn.commit()
    except Exception:
        pass
