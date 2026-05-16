import httpx, os, secrets, base64, logging
from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from models import Venda, TokenBling

logger = logging.getLogger(__name__)
BLING_AUTH_URL = "https://www.bling.com.br/Api/v3/oauth/token"
BLING_BASE_URL = "https://api.bling.com.br/Api/v3"

def gerar_url_autorizacao() -> str:
    client_id = os.getenv("BLING_CLIENT_ID")
    redirect_uri = os.getenv("BLING_REDIRECT_URI")
    state = secrets.token_hex(16)
    return (f"https://www.bling.com.br/Api/v3/oauth/authorize"
            f"?response_type=code&client_id={client_id}&redirect_uri={redirect_uri}&state={state}")

async def trocar_codigo_por_token(code: str, db: Session) -> bool:
    client_id = os.getenv("BLING_CLIENT_ID")
    client_secret = os.getenv("BLING_CLIENT_SECRET")
    redirect_uri = os.getenv("BLING_REDIRECT_URI")
    credentials = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    async with httpx.AsyncClient() as client:
        resp = await client.post(BLING_AUTH_URL,
            headers={"Authorization": f"Basic {credentials}", "Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri})
    if resp.status_code != 200:
        logger.error(f"Erro ao trocar código: {resp.text}")
        return False
    _salvar_token(resp.json(), db)
    return True

async def renovar_token(db: Session) -> bool:
    token_db = db.query(TokenBling).first()
    if not token_db:
        return False
    client_id = os.getenv("BLING_CLIENT_ID")
    client_secret = os.getenv("BLING_CLIENT_SECRET")
    credentials = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    async with httpx.AsyncClient() as client:
        resp = await client.post(BLING_AUTH_URL,
            headers={"Authorization": f"Basic {credentials}", "Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "refresh_token", "refresh_token": token_db.refresh_token})
    if resp.status_code != 200:
        return False
    _salvar_token(resp.json(), db)
    return True

def _salvar_token(data: dict, db: Session):
    expires_at = datetime.utcnow() + timedelta(seconds=data.get("expires_in", 21600))
    token_db = db.query(TokenBling).first()
    if token_db:
        token_db.access_token = data["access_token"]
        token_db.refresh_token = data["refresh_token"]
        token_db.expires_at = expires_at
        token_db.atualizado_em = datetime.utcnow()
    else:
        db.add(TokenBling(access_token=data["access_token"], refresh_token=data["refresh_token"], expires_at=expires_at))
    db.commit()

async def _get_headers(db: Session) -> dict:
    token_db = db.query(TokenBling).first()
    if not token_db:
        raise Exception("Bling não autenticado.")
    if token_db.expires_at < datetime.utcnow() + timedelta(minutes=5):
        await renovar_token(db)
        token_db = db.query(TokenBling).first()
    return {"Authorization": f"Bearer {token_db.access_token}", "Accept": "application/json"}

async def sincronizar_pedidos(db: Session, dias: int = 30) -> dict:
    headers = await _get_headers(db)
    data_inicio = (datetime.now() - timedelta(days=dias)).strftime("%Y-%m-%d")
    novos = atualizados = pagina = 0
    pagina = 1
    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            resp = await client.get(f"{BLING_BASE_URL}/pedidos/vendas", headers=headers,
                params={"pagina": pagina, "limite": 100, "dataInicial": data_inicio})
            if resp.status_code != 200:
                break
            data = resp.json()
            pedidos = data.get("data", [])
            if not pedidos:
                break
            for pedido in pedidos:
                r = _processar_pedido(pedido, db)
                if r == "novo": novos += 1
                elif r == "atualizado": atualizados += 1
            if pagina >= data.get("meta", {}).get("totalPages", 1):
                break
            pagina += 1
    db.commit()
    return {"novos": novos, "atualizados": atualizados}

def _processar_pedido(pedido: dict, db: Session) -> str:
    bling_id = str(pedido.get("id", ""))
    if not bling_id:
        return "ignorado"
    vendedor = pedido.get("vendedor") or {}
    vendedora_nome = vendedor.get("nome", "").strip() or "Sem vendedor"
    
    # Busca loja_id pelo nome da vendedora
    from models import Vendedora
    vendedora_db = db.query(Vendedora).filter(
        Vendedora.bling_vendedor_nome == vendedora_nome
    ).first()
    loja_id = vendedora_db.loja_id if vendedora_db else None

    data_str = pedido.get("data", "")
    try:
        data_venda = datetime.strptime(data_str, "%Y-%m-%d")
    except:
        data_venda = datetime.utcnow()
    contato = pedido.get("contato") or {}
    cliente_nome = contato.get("nome", "")
    valor_total = float(pedido.get("totalVenda", 0) or 0)
    num_itens = sum(int(i.get("quantidade", 0)) for i in (pedido.get("itens") or []))
    situacao_obj = pedido.get("situacao") or {}
    situacao = situacao_obj.get("nome", "") if isinstance(situacao_obj, dict) else str(situacao_obj)
    
    venda_db = db.query(Venda).filter(Venda.bling_pedido_id == bling_id).first()
    if venda_db:
        venda_db.valor_total = valor_total
        venda_db.situacao = situacao
        venda_db.loja_id = loja_id
        venda_db.sincronizado_em = datetime.utcnow()
        return "atualizado"
    db.add(Venda(bling_pedido_id=bling_id, vendedora_nome=vendedora_nome, loja_id=loja_id,
                 cliente_nome=cliente_nome, valor_total=valor_total, num_itens=num_itens,
                 data_venda=data_venda, situacao=situacao))
    return "novo"
