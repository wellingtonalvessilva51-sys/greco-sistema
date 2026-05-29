from fastapi import FastAPI, Depends, HTTPException, Request, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pydantic import BaseModel
from pathlib import Path
import os, logging, uuid
import httpx

from models import criar_tabelas, get_db, SessionLocal, Vendedora, Loja, Venda, TokenBling, Produto
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
    Path("uploads").mkdir(exist_ok=True)

app = FastAPI(title="Sistema Greco", lifespan=lifespan)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

static_dir = os.path.join(BASE_DIR, "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

uploads_dir = os.path.join(BASE_DIR, "uploads")
Path(uploads_dir).mkdir(exist_ok=True)
app.mount("/uploads", StaticFiles(directory=uploads_dir), name="uploads")

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

@app.get("/logout")
async def logout():
    resp = RedirectResponse("/")
    resp.delete_cookie("token")
    return resp

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
    vendas_mes = db.query(Venda).filter(Venda.data_venda >= inicio_mes).all()
    total_mes = sum(v.valor_total for v in vendas_mes)
    vendas_hoje = [v for v in vendas_mes if v.data_venda >= hoje]
    total_hoje = sum(v.valor_total for v in vendas_hoje)
    ranking = {}
    for v in vendas_mes:
        ranking[v.vendedora_nome] = ranking.get(v.vendedora_nome, 0) + v.valor_total
    ranking_list = sorted(ranking.items(), key=lambda x: x[1], reverse=True)[:10]
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

@app.get("/api/reset-vendas")
async def reset_vendas(request: Request, db: Session = Depends(get_db)):
    user = get_user(request, db)
    if not user or not user.is_gerente:
        raise HTTPException(403)
    db.query(Venda).delete()
    db.commit()
    return {"ok": "vendas apagadas"}

@app.post("/api/produtos")
async def receber_produto_n8n(request: Request, db: Session = Depends(get_db)):
    if request.headers.get("X-API-Key") != os.getenv("N8N_API_KEY", "modexa-n8n-2026"):
        raise HTTPException(403, "Chave inválida")
    data = await request.json()
    bling_id = str(data.get("bling_produto_id", ""))
    if not bling_id:
        raise HTTPException(400, "bling_produto_id obrigatório")
    existente = db.query(Produto).filter(Produto.bling_produto_id == bling_id).first()
    if existente:
        return {"ok": True, "id": existente.id, "novo": False}
    p = Produto(
        bling_produto_id=bling_id,
        nome=data.get("nome", ""),
        sku=data.get("sku", ""),
        gtin=data.get("gtin", ""),
        ncm=data.get("ncm", ""),
        preco_venda=float(data.get("preco_venda", 0)),
        preco_custo=float(data.get("preco_custo", 0)),
        estoque_inicial=int(data.get("estoque_inicial", 0)),
        descricao=data.get("descricao", ""),
        tem_variacoes=bool(data.get("tem_variacoes", False)),
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    return {"ok": True, "id": p.id, "bling_produto_id": p.bling_produto_id, "nome": p.nome, "novo": True}

@app.get("/cadastrar-produto", response_class=HTMLResponse)
async def cadastrar_produto_page(request: Request, db: Session = Depends(get_db)):
    user = get_user(request, db)
    if not user or not user.is_gerente:
        return RedirectResponse("/")
    return templates.TemplateResponse("cadastro_produto.html", {"request": request, "user": user})

@app.post("/api/cadastrar-produto")
async def api_cadastrar_produto(
    request: Request,
    imagem: UploadFile = File(...),
    quantidade: int = Form(...),
    custo_unitario: float = Form(...),
    tem_variacoes: str = Form("false"),
    tamanhos: str = Form(""),
    tem_cores: str = Form("false"),
    cores: str = Form(""),
    estoque_variacoes: str = Form("{}"),
    db: Session = Depends(get_db)
):
    user = get_user(request, db)
    if not user or not user.is_gerente:
        raise HTTPException(403)
    ext = (imagem.filename or "img.jpg").rsplit(".", 1)[-1].lower()
    if ext not in ("jpg", "jpeg", "png", "webp"):
        ext = "jpg"
    filename = f"{uuid.uuid4().hex}.{ext}"
    upload_path = Path(BASE_DIR) / "uploads" / filename
    upload_path.write_bytes(await imagem.read())
    base_url = os.getenv("BASE_URL", "https://greco-sistema-production.up.railway.app")
    image_url = f"{base_url}/uploads/{filename}"
    import json as _json
    tamanhos_list = [t.strip() for t in tamanhos.split(",") if t.strip()] if tamanhos else []
    cores_list = [c.strip() for c in cores.split(",") if c.strip()] if cores else []
    try:
        estoque_var_dict = _json.loads(estoque_variacoes)
    except Exception:
        estoque_var_dict = {}
    webhook_url = os.getenv("N8N_WEBHOOK_URL", "https://grecomoda.app.n8n.cloud/webhook/cadastro-produto")
    payload = {
        "image_url": image_url,
        "quantidade": quantidade,
        "custo_unitario": custo_unitario,
        "tem_variacoes": tem_variacoes.lower() == "true",
        "tamanhos": tamanhos_list,
        "tem_cores": tem_cores.lower() == "true",
        "cores": cores_list,
        "estoque_variacoes": estoque_var_dict
    }
    try:
        async with httpx.AsyncClient(timeout=90) as client:
            resp = await client.post(webhook_url, json=payload)
        if resp.status_code in (200, 202):
            try:
                return JSONResponse({"ok": True, "data": resp.json()})
            except Exception:
                return JSONResponse({"ok": True, "data": {}})
        try:
            detalhe = resp.json()
        except Exception:
            detalhe = resp.text[:500]
        return JSONResponse({"ok": False, "detail": f"n8n retornou {resp.status_code}: {detalhe}"}, status_code=200)
    except httpx.TimeoutException:
        return JSONResponse({"ok": True, "data": {}, "aviso": "Processando... verifique o Bling em instantes."})
    except Exception as e:
        return JSONResponse({"ok": False, "detail": str(e)}, status_code=200)

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.get("/dev/bling-token")
async def dev_bling_token(secret: str = "", db: Session = Depends(get_db)):
    if secret != "modexa-dev-2026":
        raise HTTPException(403)
    t = db.query(TokenBling).first()
    if not t:
        return {"error": "nenhum token encontrado"}
    return {"access_token": t.access_token, "refresh_token": t.refresh_token, "expires_at": str(t.expires_at)}
