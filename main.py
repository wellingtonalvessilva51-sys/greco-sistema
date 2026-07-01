from fastapi import FastAPI, Depends, HTTPException, Request, Form, UploadFile, File, BackgroundTasks
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pydantic import BaseModel
from pathlib import Path
import os, logging, uuid, hashlib, time, asyncio
import httpx

from models import criar_tabelas, get_db, SessionLocal, Vendedora, Loja, Venda, TokenBling, Produto, ModeloImagem, PedidoVendedorCache
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

def _load_vendor_cache_from_db():
    """Carrega cache de vendedores e itens do Postgres na inicialização."""
    try:
        db = SessionLocal()
        entries = db.query(PedidoVendedorCache).all()
        for e in entries:
            _order_vendor_cache[e.pedido_id] = e.vendor_id
            if e.total_itens is not None:
                _order_items_cache[e.pedido_id] = e.total_itens
        db.close()
        logger.info(f"Cache de vendedores carregado: {len(entries)} pedidos do banco")
    except Exception as ex:
        logger.warning(f"Não foi possível carregar cache de vendedores: {ex}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    criar_tabelas()
    _setup_inicial()
    _load_vendor_cache_from_db()
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

async def cloudinary_upload(image_bytes: bytes, folder: str, filename: str) -> str:
    cloud_name = os.getenv("CLOUDINARY_CLOUD_NAME", "")
    api_key = os.getenv("CLOUDINARY_API_KEY", "")
    api_secret = os.getenv("CLOUDINARY_API_SECRET", "")
    ts = int(time.time())
    public_id = f"{folder}/{filename}"
    sig = hashlib.sha1(f"public_id={public_id}&timestamp={ts}{api_secret}".encode()).hexdigest()
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"https://api.cloudinary.com/v1_1/{cloud_name}/image/upload",
            data={"api_key": api_key, "timestamp": str(ts), "signature": sig, "public_id": public_id},
            files={"file": ("image.png", image_bytes, "image/png")}
        )
    return resp.json()["secure_url"]

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

@app.post("/api/sincronizar/dev")
async def sincronizar_dev(secret: str = "", db: Session = Depends(get_db)):
    if secret != "modexa-dev-2026":
        raise HTTPException(403)
    resultado = await bling_svc.sincronizar_pedidos(db, dias=180)
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
        imagem_url=data.get("image_url", ""),
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    return {"ok": True, "id": p.id, "bling_produto_id": p.bling_produto_id, "nome": p.nome, "novo": True}

@app.get("/produtos", response_class=HTMLResponse)
async def listar_produtos(request: Request, db: Session = Depends(get_db)):
    user = get_user(request, db)
    if not user or not user.is_gerente:
        return RedirectResponse("/")
    produtos = db.query(Produto).order_by(Produto.criado_em.desc()).all()
    modelos_por_produto = {}
    for p in produtos:
        modelos_por_produto[p.id] = db.query(ModeloImagem).filter(ModeloImagem.produto_id == p.id).order_by(ModeloImagem.tamanho).all()
    return templates.TemplateResponse("produtos.html", {"request": request, "user": user, "produtos": produtos, "modelos": modelos_por_produto})

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
    nome_produto: str = Form(""),
    preco_venda: str = Form(""),
    sugestao_look: str = Form(""),
    db: Session = Depends(get_db)
):
    user = get_user(request, db)
    if not user or not user.is_gerente:
        raise HTTPException(403)
    ext = (imagem.filename or "img.jpg").rsplit(".", 1)[-1].lower()
    if ext not in ("jpg", "jpeg", "png", "webp"):
        ext = "jpg"
    filename = uuid.uuid4().hex
    image_bytes = await imagem.read()
    try:
        image_url = await cloudinary_upload(image_bytes, "modexa/produtos", filename)
    except Exception as e:
        logger.error(f"Cloudinary upload erro: {e}")
        base_url = os.getenv("BASE_URL", "https://greco-sistema-production.up.railway.app")
        fallback = Path(BASE_DIR) / "uploads" / f"{filename}.{ext}"
        fallback.write_bytes(image_bytes)
        image_url = f"{base_url}/uploads/{filename}.{ext}"
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
        "estoque_variacoes": estoque_var_dict,
        "nome_produto": nome_produto.strip() if nome_produto else "",
        "preco_venda_manual": float(preco_venda) if preco_venda else 0,
        "sugestao_look": sugestao_look.strip() if sugestao_look else ""
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

@app.post("/api/bling-set-estoque")
async def bling_set_estoque(request: Request):
    if request.headers.get("X-API-Key") != os.getenv("N8N_API_KEY", "modexa-n8n-2026"):
        raise HTTPException(403)
    data = await request.json()
    token = data.get("token", "")
    variacoes = data.get("variacoes", [])
    deposito_id = int(data.get("deposito_id", 14887000136))
    results = []
    async with httpx.AsyncClient(timeout=30) as client:
        for v in variacoes:
            qty = float(v.get("quantidade", 0))
            if qty <= 0:
                continue
            payload = {
                "produto": {"id": int(v["id"])},
                "deposito": {"id": deposito_id},
                "quantidade": qty,
                "operacao": "B"
            }
            resp = await client.post(
                "https://www.bling.com.br/Api/v3/estoques",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json", "Accept": "application/json"},
                json=payload
            )
            results.append({"id": v["id"], "status": resp.status_code, "ok": resp.status_code in (200, 201)})
    return {"ok": True, "results": results}

async def _atualizar_imagens_bling(bling_id: str, urls: list, db: Session):
    try:
        headers = await bling_svc._get_headers(db)
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(f"https://www.bling.com.br/Api/v3/produtos/{bling_id}", headers=headers)
            if r.status_code != 200:
                logger.error(f"Bling GET produto {bling_id}: {r.status_code}")
                return
            prod = r.json().get("data", {})
            externas_atuais = (prod.get("midia") or {}).get("imagens", {}).get("externas") or []
            externas_novas = [e for e in externas_atuais if e.get("link")] + [{"link": u} for u in urls]
            payload = {
                "nome": prod.get("nome", ""),
                "tipo": prod.get("tipo", "P"),
                "situacao": prod.get("situacao", "A"),
                "formato": prod.get("formato", "S"),
                "preco": prod.get("preco", 0),
                "unidade": "Un",
                "condicao": 1,
                "linhaProduto": {"id": (prod.get("linhaProduto") or {}).get("id", 1) or 1},
                "midia": {"imagens": {"externas": externas_novas}},
            }
            if prod.get("formato") == "V":
                variacoes_put = []
                for v in (prod.get("variacoes") or []):
                    v_put = {
                        "nome": prod.get("nome", ""),
                        "tipo": "P",
                        "situacao": v.get("situacao", "A"),
                        "formato": "S",
                        "codigo": v.get("codigo", ""),
                        "preco": v.get("preco", prod.get("preco", 0)),
                    }
                    variacao_sub = v.get("variacao") or {}
                    vnome = variacao_sub.get("nome") or ""
                    if not vnome:
                        vnome = v.get("nome", "").replace(prod.get("nome", "") + " ", "", 1)
                    if vnome:
                        v_put["variacao"] = {"nome": vnome}
                    if v.get("gtin"):
                        v_put["gtin"] = v["gtin"]
                        v_put["gtinEmbalagem"] = v["gtin"]
                    variacoes_put.append(v_put)
                if variacoes_put:
                    payload["variacoes"] = variacoes_put
            put_r = await client.put(
                f"https://www.bling.com.br/Api/v3/produtos/{bling_id}",
                headers={**headers, "Content-Type": "application/json"},
                json=payload,
            )
            if put_r.status_code == 200:
                logger.info(f"Imagens do produto {bling_id} atualizadas no Bling")
            else:
                logger.error(f"Bling PUT imagens {bling_id}: {put_r.status_code} - {put_r.text[:200]}")
    except Exception as e:
        logger.error(f"_atualizar_imagens_bling: {e}")


@app.post("/api/gerar-imagens-modelo")
async def gerar_imagens_modelo(request: Request, db: Session = Depends(get_db)):
    if request.headers.get("X-API-Key") != os.getenv("N8N_API_KEY", "modexa-n8n-2026"):
        raise HTTPException(403)
    data = await request.json()
    bling_id = str(data.get("bling_produto_id", ""))
    modelos = data.get("modelos", [])
    openai_key = os.getenv("OPENAI_API_KEY", "")
    if not openai_key:
        return {"ok": False, "error": "OPENAI_API_KEY nao configurada no Railway"}
    produto = db.query(Produto).filter(Produto.bling_produto_id == bling_id).first()
    if not produto:
        return {"ok": False, "error": "Produto nao encontrado"}
    base_url = os.getenv("BASE_URL", "https://greco-sistema-production.up.railway.app")
    resultados = []
    async with httpx.AsyncClient(timeout=300) as client:
        for modelo in modelos:
            prompt = modelo.get("prompt", "")
            tamanho = modelo.get("tamanho", "")
            tipo = modelo.get("tipo", "")
            if not prompt:
                continue
            try:
                resp = await client.post(
                    "https://api.openai.com/v1/images/generations",
                    headers={"Authorization": f"Bearer {openai_key}", "Content-Type": "application/json"},
                    json={"model": "gpt-image-1", "prompt": prompt, "n": 1, "size": "1024x1024", "quality": "high"}
                )
                img_data = resp.json()
                if "data" not in img_data:
                    openai_err = img_data.get("error", {})
                    msg = openai_err.get("message", str(img_data)) if isinstance(openai_err, dict) else str(openai_err)
                    raise Exception(f"OpenAI: {msg}")
                import base64
                b64_str = img_data["data"][0]["b64_json"]
                img_bytes = base64.b64decode(b64_str)
                if img_bytes:
                    fname = f"modelo_{produto.id}_{tamanho}_{tipo}_{uuid.uuid4().hex[:8]}"
                    try:
                        saved_url = await cloudinary_upload(img_bytes, "modexa/modelos", fname)
                    except Exception as ce:
                        logger.error(f"Cloudinary modelo upload erro: {ce}")
                        path = Path(BASE_DIR) / "uploads" / f"{fname}.png"
                        path.write_bytes(img_bytes)
                        base_url = os.getenv("BASE_URL", "https://greco-sistema-production.up.railway.app")
                        saved_url = f"{base_url}/uploads/{fname}.png"
                    db.add(ModeloImagem(
                        produto_id=produto.id,
                        tamanho=tamanho,
                        tipo_modelo=tipo,
                        imagem_url=saved_url,
                        prompt_usado=prompt
                    ))
                    resultados.append({"tamanho": tamanho, "tipo": tipo, "url": saved_url, "ok": True})
            except Exception as e:
                logger.error(f"Erro imagem modelo {tamanho}: {e}")
                resultados.append({"tamanho": tamanho, "tipo": tipo, "ok": False, "error": str(e)})
    db.commit()
    urls_ok = [r["url"] for r in resultados if r.get("ok") and r.get("url")]
    if bling_id and urls_ok:
        await _atualizar_imagens_bling(bling_id, urls_ok, db)
    return {"ok": True, "resultados": resultados}

@app.get("/api/bling/contatos")
async def bling_contatos(pagina: int = 1, limite: int = 100, pesquisa: str = "", tipo: str = "", situacao: str = "", db: Session = Depends(get_db)):
    try:
        headers = await bling_svc._get_headers(db)
        params: dict = {"pagina": pagina, "limite": limite}
        if pesquisa:  params["pesquisa"]  = pesquisa
        if tipo:      params["tipoPessoa"] = tipo        # F = Física, J = Jurídica
        if situacao:  params["situacao"]  = situacao   # A = Ativo, I = Inativo
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(f"{bling_svc.BLING_BASE_URL}/contatos", headers=headers, params=params)
        if resp.status_code != 200:
            return {"error": f"Bling retornou {resp.status_code}", "detail": resp.text}
        return resp.json()
    except Exception as e:
        return {"error": str(e)}

@app.get("/api/vendedoras")
async def api_vendedoras(db: Session = Depends(get_db)):
    rows = db.query(Vendedora).filter(Vendedora.ativa == True).order_by(Vendedora.nome).all()
    return [{"id": v.id, "nome": v.nome, "bling_nome": v.bling_vendedor_nome or v.nome} for v in rows]

@app.get("/api/bling/vendedores")
async def api_bling_vendedores(db: Session = Depends(get_db)):
    """Retorna lista de vendedores cadastrados no Bling com id e nome."""
    try:
        headers = await bling_svc._get_headers(db)
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(f"{bling_svc.BLING_BASE_URL}/vendedores", headers=headers)
        if resp.status_code != 200:
            return []
        data = resp.json().get("data", [])
        return [{"id": v["id"], "nome": (v.get("contato") or {}).get("nome", "")} for v in data if (v.get("contato") or {}).get("nome")]
    except Exception:
        return []

@app.get("/api/bling/lojas")
async def api_bling_lojas(db: Session = Depends(get_db)):
    """Retorna todas as lojas conhecidas + novas descobertas nos pedidos recentes."""
    NOMES_CONHECIDOS = {
        204510878: "Taboão",
        205836734: "Campo Limpo",
        204889853: "Gibran",
        204494144: "Mercado Livre",
        204993060: "Site Nuvemshop",
    }
    try:
        headers = await bling_svc._get_headers(db)
        from datetime import date, timedelta
        data_ini = (date.today() - timedelta(days=60)).isoformat()
        # começa com todas as lojas conhecidas
        lojas: dict = dict(NOMES_CONHECIDOS)
        async with httpx.AsyncClient(timeout=15) as client:
            for pg in range(1, 4):
                resp = await client.get(
                    f"{bling_svc.BLING_BASE_URL}/pedidos/vendas",
                    headers=headers,
                    params={"pagina": pg, "limite": 100, "dataInicial": data_ini}
                )
                if resp.status_code != 200:
                    break
                pedidos = resp.json().get("data", [])
                if not pedidos:
                    break
                for p in pedidos:
                    lid = (p.get("loja") or {}).get("id")
                    if lid and lid not in lojas:
                        lojas[lid] = f"Loja {lid}"
        return [{"id": k, "nome": v} for k, v in sorted(lojas.items(), key=lambda x: x[1])]
    except Exception:
        return [{"id": k, "nome": v} for k, v in NOMES_CONHECIDOS.items()]

@app.get("/api/bling/vendas/total")
async def bling_vendas_total(contato: str = "", vendedorId: str = "", lojaId: str = "", dataInicial: str = "", dataFinal: str = "", db: Session = Depends(get_db)):
    """Soma todos os pedidos do filtro (percorre todas as páginas) e retorna totalValor e totalPedidos."""
    try:
        headers = await bling_svc._get_headers(db)
        limite = 100
        params_base: dict = {"limite": limite}
        if contato:     params_base["contato[nome]"] = contato
        if vendedorId:  params_base["idVendedor"] = vendedorId
        if lojaId:      params_base["idLoja"] = lojaId
        if dataInicial: params_base["dataInicial"] = dataInicial
        if dataFinal:   params_base["dataFinal"] = dataFinal

        total_valor = 0.0
        total_pedidos = 0
        pagina = 1
        async with httpx.AsyncClient(timeout=30) as client:
            while pagina <= 50:
                tentativas = 0
                resp = None
                while tentativas < 3:
                    resp = await client.get(
                        f"{bling_svc.BLING_BASE_URL}/pedidos/vendas",
                        headers=headers,
                        params={**params_base, "pagina": pagina}
                    )
                    if resp.status_code == 200:
                        break
                    tentativas += 1
                    await asyncio.sleep(1.0)  # aguarda 1s antes de retentar (rate limit)
                if resp is None or resp.status_code != 200:
                    break
                data = resp.json()
                pedidos = data.get("data", [])
                if not pedidos:
                    break
                for p in pedidos:
                    sit = p.get("situacao", {})
                    sit_id = sit.get("id") if isinstance(sit, dict) else sit
                    if sit_id in (12, 24):  # Cancelado, Devolvido
                        continue
                    total_valor += float(p.get("total") or p.get("totalVenda") or 0)
                total_pedidos += len(pedidos)
                # Bling não retorna meta.totalPages — paramos quando a página vem incompleta
                if len(pedidos) < limite:
                    break
                pagina += 1
                await asyncio.sleep(0.3)  # pequena pausa para não acionar rate limit do Bling
        return {"totalValor": round(total_valor, 2), "totalPedidos": total_pedidos}
    except Exception as e:
        return {"error": str(e)}

# Cache em memória para mapa id→nome de vendedores (TTL 1h)
_vendor_name_cache: dict = {"_ts": 0.0}
# Cache id_pedido → id_vendedor (None = sem vendedor, ausente = ainda não buscado)
_order_vendor_cache: dict = {}
# Cache id_pedido → total de itens (peças) do pedido individual
_order_items_cache: dict = {}

async def _warm_vendor_cache_bg(pids: list, headers: dict):
    """Busca vendor_id e item counts em background; persiste no Postgres para sobreviver restarts."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            for pid in pids:
                if pid in _order_vendor_cache and pid in _order_items_cache:
                    continue
                vid = None
                total_itens = 0.0
                try:
                    r = await client.get(f"{bling_svc.BLING_BASE_URL}/pedidos/vendas/{pid}", headers=headers)
                    data = r.json().get("data", {})
                    vid = (data.get("vendedor") or {}).get("id")
                    itens = data.get("itens") or []
                    total_itens = sum(float(it.get("quantidade") or 0) for it in itens)
                except Exception:
                    pass
                _order_vendor_cache[pid] = vid
                _order_items_cache[pid] = total_itens
                # Persiste no banco
                try:
                    db = SessionLocal()
                    entry = db.query(PedidoVendedorCache).filter(PedidoVendedorCache.pedido_id == pid).first()
                    if entry:
                        entry.vendor_id = vid
                        entry.total_itens = total_itens
                        entry.cached_at = datetime.utcnow()
                    else:
                        db.add(PedidoVendedorCache(pedido_id=pid, vendor_id=vid, total_itens=total_itens))
                    db.commit()
                    db.close()
                except Exception:
                    pass
                await asyncio.sleep(0.35)
    except Exception:
        pass

async def _get_vendor_name_map(headers: dict) -> dict:
    now = time.time()
    if now - _vendor_name_cache["_ts"] < 3600 and len(_vendor_name_cache) > 1:
        return {k: v for k, v in _vendor_name_cache.items() if k != "_ts"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{bling_svc.BLING_BASE_URL}/vendedores", headers=headers)
        data = resp.json().get("data", [])
        m = {v["id"]: (v.get("contato") or {}).get("nome", "") for v in data if (v.get("contato") or {}).get("nome")}
        _vendor_name_cache.clear()
        _vendor_name_cache.update(m)
        _vendor_name_cache["_ts"] = now
        return m
    except Exception:
        return {}

@app.get("/api/bling/vendas")
async def bling_vendas(pagina: int = 1, limite: int = 50, contato: str = "", vendedor: str = "", vendedorId: str = "", lojaId: str = "", dataInicial: str = "", dataFinal: str = "", background_tasks: BackgroundTasks = None, db: Session = Depends(get_db)):
    try:
        headers = await bling_svc._get_headers(db)
        params: dict = {"pagina": pagina, "limite": limite}
        if contato:
            params["contato[nome]"] = contato
        if vendedorId:
            params["idVendedor"] = vendedorId
        elif vendedor:
            params["idVendedor"] = vendedor
        if lojaId:
            params["idLoja"] = lojaId
        if dataInicial:
            params["dataInicial"] = dataInicial
        if dataFinal:
            params["dataFinal"] = dataFinal
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(f"{bling_svc.BLING_BASE_URL}/pedidos/vendas", headers=headers, params=params)
        if resp.status_code != 200:
            return {"error": f"Bling retornou {resp.status_code}", "detail": resp.text}
        data = resp.json()
        pedidos = data.get("data", [])

        # Enriquece pedidos com nome da vendedora — usa cache imediatamente, busca resto em background
        if pedidos:
            try:
                vendor_map = await _get_vendor_name_map(headers)
                # Aplica cache existente já nesta resposta
                for p in pedidos:
                    if p["id"] in _order_vendor_cache:
                        vid = _order_vendor_cache[p["id"]]
                        nome = vendor_map.get(vid, "") if vid else ""
                        if nome:
                            p["vendedor"] = {"nome": nome}
                # Agenda busca dos não cacheados em background (próxima carga terá os nomes)
                ids_sem_cache = [p["id"] for p in pedidos if p["id"] not in _order_vendor_cache]
                if ids_sem_cache and background_tasks is not None:
                    background_tasks.add_task(_warm_vendor_cache_bg, ids_sem_cache, headers)
            except Exception:
                pass

        return data
    except Exception as e:
        return {"error": str(e)}

@app.get("/api/bling/ranking-vendedoras")
async def bling_ranking_vendedoras(dataInicial: str = "", dataFinal: str = "", lojaId: str = "", background_tasks: BackgroundTasks = None, db: Session = Depends(get_db)):
    try:
        headers = await bling_svc._get_headers(db)
        vendor_map = await _get_vendor_name_map(headers)
        if not vendor_map:
            return []

        # Fase 1: busca listas de pedidos por vendedora (valor, pedidos, IDs)
        PA_SAMPLE = 5  # pedidos individuais por vendedora para estimar PA
        vendor_buckets: dict = {}  # vendor_id -> {nome, totalValor, totalPedidos, ticketMedio, order_ids}
        async with httpx.AsyncClient(timeout=15) as client:
            for vendor_id, vendor_nome in vendor_map.items():
                pagina = 1
                total_valor = 0.0
                total_pedidos = 0
                order_ids: list = []
                while True:
                    params: dict = {"pagina": pagina, "limite": 100, "idVendedor": vendor_id}
                    if dataInicial: params["dataInicial"] = dataInicial
                    if dataFinal:   params["dataFinal"]   = dataFinal
                    if lojaId:      params["idLoja"]      = lojaId
                    try:
                        r = await client.get(f"{bling_svc.BLING_BASE_URL}/pedidos/vendas", headers=headers, params=params)
                        pedidos = r.json().get("data", []) if r.status_code == 200 else []
                    except Exception:
                        break
                    for p in pedidos:
                        sit = p.get("situacao") or {}
                        sit_id = sit.get("id") if isinstance(sit, dict) else sit
                        if sit_id in (12, 24):
                            continue
                        total_valor += float(p.get("total") or p.get("totalVenda") or 0)
                        total_pedidos += 1
                        order_ids.append(p["id"])
                    if len(pedidos) < 100:
                        break
                    pagina += 1
                    await asyncio.sleep(0.3)
                if total_pedidos > 0:
                    vendor_buckets[vendor_id] = {
                        "nome": vendor_nome,
                        "totalValor": round(total_valor, 2),
                        "totalPedidos": total_pedidos,
                        "ticketMedio": round(total_valor / total_pedidos, 2),
                        "order_ids": order_ids,
                    }
                await asyncio.sleep(0.3)

        # Fase 2: busca síncrona de amostra de PA_SAMPLE pedidos por vendedora
        sample_fetch = []
        for vb in vendor_buckets.values():
            for pid in vb["order_ids"][:PA_SAMPLE]:
                if pid not in _order_items_cache:
                    sample_fetch.append(pid)

        db_rank = SessionLocal()
        async with httpx.AsyncClient(timeout=10) as client:
            for pid in sample_fetch:
                if pid in _order_items_cache:
                    continue
                vid = None
                total_itens = 0.0
                try:
                    r = await client.get(f"{bling_svc.BLING_BASE_URL}/pedidos/vendas/{pid}", headers=headers)
                    data = r.json().get("data", {})
                    vid = (data.get("vendedor") or {}).get("id")
                    itens = data.get("itens") or []
                    total_itens = sum(float(it.get("quantidade") or 0) for it in itens)
                except Exception:
                    pass
                _order_vendor_cache[pid] = vid
                _order_items_cache[pid] = total_itens
                try:
                    entry = db_rank.query(PedidoVendedorCache).filter(PedidoVendedorCache.pedido_id == pid).first()
                    if entry:
                        entry.vendor_id = vid; entry.total_itens = total_itens; entry.cached_at = datetime.utcnow()
                    else:
                        db_rank.add(PedidoVendedorCache(pedido_id=pid, vendor_id=vid, total_itens=total_itens))
                    db_rank.commit()
                except Exception:
                    db_rank.rollback()
                await asyncio.sleep(0.35)
        db_rank.close()

        # Fase 3: monta ranking com PA estimado da amostra
        ranking = []
        remaining_ids: list = []
        for vendor_id, vb in vendor_buckets.items():
            sample_pids = vb["order_ids"][:PA_SAMPLE]
            cached = [pid for pid in sample_pids if pid in _order_items_cache]
            if cached:
                avg_itens = sum(_order_items_cache[pid] for pid in cached) / len(cached)
                total_itens = int(avg_itens * vb["totalPedidos"])
                pa = round(avg_itens, 2)
                pa_aproximado = vb["totalPedidos"] > PA_SAMPLE
            else:
                total_itens = 0
                pa = None
                pa_aproximado = False
            remaining_ids.extend(pid for pid in vb["order_ids"][PA_SAMPLE:] if pid not in _order_items_cache)
            ranking.append({
                "id": vendor_id,
                "nome": vb["nome"],
                "totalValor": vb["totalValor"],
                "totalPedidos": vb["totalPedidos"],
                "totalItens": total_itens,
                "ticketMedio": vb["ticketMedio"],
                "pa": pa,
                "paAproximado": pa_aproximado,
            })

        # Background: aquece cache dos pedidos restantes para próximas cargas
        if remaining_ids and background_tasks is not None:
            background_tasks.add_task(_warm_vendor_cache_bg, remaining_ids, headers)

        ranking.sort(key=lambda x: x["totalValor"], reverse=True)
        return ranking
    except Exception as e:
        return {"error": str(e)}

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
    from datetime import datetime, timedelta
    if t.expires_at < datetime.utcnow() + timedelta(minutes=10):
        ok = await bling_svc.renovar_token(db)
        if ok:
            db.refresh(t)
        else:
            return {"error": "token expirado e renovacao falhou — refaca o login em /auth/bling"}
    return {"access_token": t.access_token, "refresh_token": t.refresh_token, "expires_at": str(t.expires_at)}
