from fastapi import FastAPI, Depends, HTTPException, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pydantic import BaseModel
import os, logging

from models import criar_tabelas, get_db, SessionLocal, Vendedora, Loja, Venda, TokenBling
from auth import hash_senha, verificar_senha, criar_token, verificar_token
import bling as bling_svc

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
scheduler = AsyncIOScheduler()

async def job_sincronizar():
    db = SessionLocal()
    try:
        await bling_svc.sincronizar_pedidos(db, dias=35)
    except Exception as e:
        logger.error(f"Erro sync: {e}")
    finally:
        db.close()

@asynccontextmanager
async def lifespan(app: FastAPI):
    criar_tabelas()
    _setup_inicial()
    scheduler.add_job(job_sincronizar, "interval", hours=1)
    scheduler.start()
    yield
    scheduler.shutdown()

def _setup_inicial():
    db = SessionLocal()
    try:
        if not db.query(Vendedora).filter(Vendedora.is_gerente == True).first():
            email = os.getenv("EMAIL_GERENTE", "gerente@greco.com")
            senha = os.getenv("SENHA_GERENTE", "greco@2024")
            db.add(Vendedora(nome="Gerente", email=email, senha_hash=hash_senha(senha), is_gerente=True, ativa=True))
            db.commit()
            logger.info(f"Gerente criada: {email}")
    finally:
        db.close()

app = FastAPI(title="Sistema Greco", lifespan=lifespan)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

static_dir = os.path.join(BASE_DIR, "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

# ── Helpers ──────────────────────────────────────────────

def get_user(request: Request, db: Session):
    token = request.cookies.get("token")
    if not token:
        return None
    payload = verificar_token(token)
    if not payload:
        return None
    return db.query(Vendedora).filter(Vendedora.id == int(payload["sub"])).first()

def fmt_brl(v):
    return f"R$ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

# ── Auth ─────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def login_page(request: Request, db: Session = Depends(get_db)):
    user = get_user(request, db)
    if user:
        return RedirectResponse("/gerente" if user.is_gerente else "/vendedora")
    return templates.TemplateResponse("login.html", {"request": request, "erro": ""})

@app.post("/login")
async def login(request: Request, email: str = Form(...), senha: str = Form(...), db: Session = Depends(get_db)):
    user = db.query(Vendedora).filter(Vendedora.email == email.lower().strip(), Vendedora.ativa == True).first()
    if not user or not verificar_senha(senha, user.senha_hash):
        return templates.TemplateResponse("login.html", {"request": request, "erro": "E-mail ou senha incorretos."})
    token = criar_token(user.id, user.is_gerente)
    resp = RedirectResponse("/gerente" if user.is_gerente else "/vendedora", status_code=303)
    resp.set_cookie("token", token, max_age=30*24*3600, httponly=True)
    return resp
@app.get("/api/debug-bling")
async def debug_bling(request: Request, db: Session = Depends(get_db)):
    user = get_user(request, db)
    if not user or not user.is_gerente:
        raise HTTPException(403)
    from models import TokenBling
    from datetime import datetime
    token = db.query(TokenBling).first()
    if not token:
        return {"erro": "Sem token"}
    return {
        "tem_token": True,
        "expires_at": str(token.expires_at),
        "expirado": token.expires_at < datetime.utcnow(),
        "access_token_inicio": token.access_token[:20] if token.access_token else None
    }
@app.get("/logout")
async def logout():
    resp = RedirectResponse("/")
    resp.delete_cookie("token")
    return resp

# ── Dashboard Gerente ─────────────────────────────────────

@app.get("/gerente", response_class=HTMLResponse)
async def gerente_dashboard(request: Request, db: Session = Depends(get_db)):
    user = get_user(request, db)
    if not user or not user.is_gerente:
        return RedirectResponse("/")
    
    agora = datetime.now()
    inicio_mes = agora.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    hoje = agora.replace(hour=0, minute=0, second=0, microsecond=0)
    
    lojas = db.query(Loja).filter(Loja.ativa == True).all()
    vendedoras = db.query(Vendedora).filter(Vendedora.is_gerente == False, Vendedora.ativa == True).all()
    
    # Vendas do mês
    vendas_mes = db.query(Venda).filter(Venda.data_venda >= inicio_mes).all()
    total_mes = sum(v.valor_total for v in vendas_mes)
    
    # Vendas hoje
    vendas_hoje = [v for v in vendas_mes if v.data_venda >= hoje]
    total_hoje = sum(v.valor_total for v in vendas_hoje)
    
    # Ranking vendedoras
    ranking = {}
    for v in vendas_mes:
        ranking[v.vendedora_nome] = ranking.get(v.vendedora_nome, 0) + v.valor_total
    ranking_list = sorted(ranking.items(), key=lambda x: x[1], reverse=True)[:10]
    
    # Stats por loja
    lojas_stats = []
    for loja in lojas:
        vendas_loja = [v for v in vendas_mes if v.loja_id == loja.id]
        fat_loja = sum(v.valor_total for v in vendas_loja)
        pct = int(fat_loja / loja.meta_mensal * 100) if loja.meta_mensal > 0 else 0
        lojas_stats.append({
            "nome": loja.nome,
            "faturamento": fmt_brl(fat_loja),
            "meta": fmt_brl(loja.meta_mensal),
            "pct": min(pct, 100),
            "pct_num": pct
        })
    
    return templates.TemplateResponse("gerente.html", {
        "request": request,
        "user": user,
        "total_mes": fmt_brl(total_mes),
        "total_hoje": fmt_brl(total_hoje),
        "num_vendedoras": len(vendedoras),
        "num_lojas": len(lojas),
        "ranking": [(n, fmt_brl(v)) for n, v in ranking_list],
        "lojas_stats": lojas_stats,
        "vendedoras": vendedoras,
        "lojas": lojas,
    })

# ── Dashboard Vendedora ───────────────────────────────────

@app.get("/vendedora", response_class=HTMLResponse)
async def vendedora_dashboard(request: Request, db: Session = Depends(get_db)):
    user = get_user(request, db)
    if not user or user.is_gerente:
        return RedirectResponse("/")
    
    agora = datetime.now()
    inicio_mes = agora.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    hoje = agora.replace(hour=0, minute=0, second=0, microsecond=0)
    
    vendas_mes = db.query(Venda).filter(
        Venda.data_venda >= inicio_mes,
        Venda.vendedora_nome == user.bling_vendedor_nome
    ).all() if user.bling_vendedor_nome else []
    
    fat_mes = sum(v.valor_total for v in vendas_mes)
    fat_hoje = sum(v.valor_total for v in vendas_mes if v.data_venda >= hoje)
    pecas_mes = sum(v.num_itens for v in vendas_mes)
    comissao = fat_mes * user.percentual_comissao / 100
    pct_meta = int(fat_mes / user.meta_mensal * 100) if user.meta_mensal > 0 else 0
    
    # Ranking da loja
    if user.loja_id:
        vendas_loja = db.query(Venda).filter(Venda.data_venda >= inicio_mes, Venda.loja_id == user.loja_id).all()
        ranking = {}
        for v in vendas_loja:
            ranking[v.vendedora_nome] = ranking.get(v.vendedora_nome, 0) + v.valor_total
        ranking_list = sorted(ranking.items(), key=lambda x: x[1], reverse=True)
    else:
        ranking_list = []
    
    return templates.TemplateResponse("vendedora.html", {
        "request": request,
        "user": user,
        "fat_mes": fmt_brl(fat_mes),
        "fat_hoje": fmt_brl(fat_hoje),
        "pecas_mes": pecas_mes,
        "comissao": fmt_brl(comissao),
        "pct_meta": min(pct_meta, 100),
        "pct_meta_num": pct_meta,
        "meta": fmt_brl(user.meta_mensal),
        "ranking": [(n, fmt_brl(v), n == user.bling_vendedor_nome) for n, v in ranking_list],
    })

# ── API Gerente ───────────────────────────────────────────

@app.post("/api/sincronizar")
async def sincronizar(request: Request, db: Session = Depends(get_db)):
    user = get_user(request, db)
    if not user or not user.is_gerente:
        raise HTTPException(403)
    resultado = await bling_svc.sincronizar_pedidos(db, dias=35)
    return resultado

@app.post("/api/lojas")
async def criar_loja(request: Request, nome: str = Form(...), meta: float = Form(0), db: Session = Depends(get_db)):
    user = get_user(request, db)
    if not user or not user.is_gerente:
        raise HTTPException(403)
    db.add(Loja(nome=nome, meta_mensal=meta))
    db.commit()
    return RedirectResponse("/gerente", status_code=303)

@app.post("/api/lojas/{loja_id}/meta")
async def atualizar_meta_loja(loja_id: int, request: Request, meta: float = Form(...), db: Session = Depends(get_db)):
    user = get_user(request, db)
    if not user or not user.is_gerente:
        raise HTTPException(403)
    loja = db.query(Loja).filter(Loja.id == loja_id).first()
    if loja:
        loja.meta_mensal = meta
        db.commit()
    return RedirectResponse("/gerente", status_code=303)

@app.post("/api/vendedoras")
async def criar_vendedora(request: Request, nome: str = Form(...), email: str = Form(...),
    senha: str = Form(...), loja_id: int = Form(0), bling_nome: str = Form(""),
    meta: float = Form(0), comissao: float = Form(5), db: Session = Depends(get_db)):
    user = get_user(request, db)
    if not user or not user.is_gerente:
        raise HTTPException(403)
    v = Vendedora(nome=nome, email=email.lower(), senha_hash=hash_senha(senha),
                  loja_id=loja_id if loja_id > 0 else None,
                  bling_vendedor_nome=bling_nome, meta_mensal=meta,
                  percentual_comissao=comissao, is_gerente=False, ativa=True)
    db.add(v)
    db.commit()
    return RedirectResponse("/gerente", status_code=303)

@app.post("/api/vendedoras/{vid}/meta")
async def atualizar_meta_vendedora(vid: int, request: Request, meta: float = Form(...),
    comissao: float = Form(5), db: Session = Depends(get_db)):
    user = get_user(request, db)
    if not user or not user.is_gerente:
        raise HTTPException(403)
    v = db.query(Vendedora).filter(Vendedora.id == vid).first()
    if v:
        v.meta_mensal = meta
        v.percentual_comissao = comissao
        db.commit()
    return RedirectResponse("/gerente", status_code=303)

# ── Bling OAuth ───────────────────────────────────────────

@app.get("/auth/bling")
async def iniciar_bling():
    return RedirectResponse(bling_svc.gerar_url_autorizacao())

@app.get("/auth/bling/callback")
async def callback_bling(code: str = None, state: str = None, error: str = None, db: Session = Depends(get_db)):
    if error or not code:
        raise HTTPException(400, f"Erro: {error or 'código não recebido'}")
    ok = await bling_svc.trocar_codigo_por_token(code, db)
    if not ok:
        raise HTTPException(500, "Erro ao conectar Bling.")
    return HTMLResponse("<h2>✅ Bling conectado! Pode fechar esta janela.</h2>")

@app.get("/health")
async def health():
    return {"status": "ok"}
