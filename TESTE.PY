import pandas as pd
import os
import logging
import asyncio
import httpx
import gdown
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, date
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler, MessageHandler, filters
from telegram.request import HTTPXRequest
from telegram.error import TimedOut, NetworkError, BadRequest

# ── Patch SSL global (proxy corporativo) ──────────────────────────────────
import httpx as _httpx
_orig_async_init = _httpx.AsyncClient.__init__
_orig_sync_init  = _httpx.Client.__init__

def _async_no_ssl(self, *a, **kw):
    kw['verify'] = False
    _orig_async_init(self, *a, **kw)

def _sync_no_ssl(self, *a, **kw):
    kw['verify'] = False
    _orig_sync_init(self, *a, **kw)

_httpx.AsyncClient.__init__ = _async_no_ssl
_httpx.Client.__init__      = _sync_no_ssl

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ── SERVIDOR WEB FANTASMA (Para o Render manter o bot ligado) ──────────────
class ServidorFantasma(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot do Telegram operando normalmente!")

def iniciar_servidor():
    porta = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", porta), ServidorFantasma)
    server.serve_forever()

threading.Thread(target=iniciar_servidor, daemon=True).start()

# ── Configuração de Diretórios ─────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Mantemos o nome original do seu arquivo do Google Drive (mude aqui se for .xlsx ou .xlsb)
ARQUIVO_BASE = os.path.join(BASE_DIR, "BASE.xlsb")

# ── Carrega base principal otimizada (Lendo as duas abas sem travar) ───────
ID_DO_DRIVE = "1_wQXyZDjesv2lPMdIla_suxT6n86eM_o"
URL_DOWNLOAD = f"https://drive.google.com/uc?id={ID_DO_DRIVE}"

try:
    if not os.path.exists(ARQUIVO_BASE):
        logger.info("🔄 Arquivo BASE não encontrado. Baixando do Google Drive...")
        gdown.download(URL_DOWNLOAD, ARQUIVO_BASE, quiet=False)
        logger.info("✅ Download concluído!")

    logger.info("📊 Lendo as abas do arquivo de forma otimizada...")
    
    # Lê todas as abas de uma vez, mas convertendo colunas para string para economizar RAM
    # Se o seu arquivo for .xlsx, mude engine para 'openpyxl'. Se for .xlsb, deixe 'pyxlsb'
    engine_usado = 'pyxlsb' if ARQUIVO_BASE.endswith('.xlsb') else 'openpyxl'
    
    todas_abas = pd.read_excel(ARQUIVO_BASE, sheet_name=None, engine=engine_usado, dtype=str)
    df = pd.concat(todas_abas.values(), ignore_index=True)
    
    # Limpa espaços em branco dos nomes das colunas para evitar erros de leitura
    df.columns = df.columns.str.strip()
    
    logger.info(f"✅ Base carregada com sucesso! Total de registros: {len(df)}")
    
except Exception as e:
    logger.error(f"❌ Erro ao baixar ou carregar o arquivo: {e}")
    df = pd.DataFrame()

# ── Gerenciador de mensagens (NA MEMÓRIA) ──────────────────────────────────
class GerenciadorMensagens:
    def __init__(self):
        self.mensagens = []

    def adicionar_mensagem(self, chat_id, message_ids, data_hora_atual):
        nova = {'chat_id': chat_id, 'message_ids': message_ids, 'data_hora': data_hora_atual}
        self.mensagens.append(nova)
        return nova

    async def enviar_mensagem_com_retry(self, bot, chat_id, text, max_tentativas=3, parse_mode=None, reply_markup=None):
        for tentativa in range(max_tentativas):
            try:
                return await bot.send_message(
                    chat_id=chat_id, text=text,
                    parse_mode=parse_mode, reply_markup=reply_markup
                )
            except (TimedOut, NetworkError):
                if tentativa < max_tentativas - 1:
                    await asyncio.sleep(2 ** tentativa)
                else:
                    raise

    async def deletar_mensagem_com_retry(self, bot, chat_id, message_id, max_tentativas=3):
        for tentativa in range(max_tentativas):
            try:
                await bot.delete_message(chat_id=chat_id, message_id=message_id)
                return True
            except (TimedOut, NetworkError):
                if tentativa < max_tentativas - 1:
                    await asyncio.sleep(1 + tentativa)
                else:
                    return False
            except BadRequest:
                return False
            except Exception as e:
                logger.warning(f"⚠️ Erro inesperado ao deletar {message_id}: {e}")
                return False

    async def limpar_mensagens_diarias(self, application):
        hoje = date.today()
        antigas = [m for m in self.mensagens if m['data_hora'].date() < hoje]
        if not antigas:
            return
        total = 0
        for msg_info in antigas:
            for mid in msg_info['message_ids']:
                if await self.deletar_mensagem_com_retry(application.bot, msg_info['chat_id'], mid):
                    total += 1
        self.mensagens = [m for m in self.mensagens if m['data_hora'].date() >= hoje]

    async def limpar_mensagens_teste(self, application):
        if not self.mensagens:
            return
        total = 0
        for msg_info in list(self.mensagens):
            for mid in msg_info['message_ids']:
                if await self.deletar_mensagem_com_retry(application.bot, msg_info['chat_id'], mid):
                    total += 1
        self.mensagens = []

    def obter_estatisticas(self):
        hoje = date.today()
        return {
            'hoje': len([m for m in self.mensagens if m['data_hora'].date() == hoje]),
            'total': len(self.mensagens)
        }

gerenciador = GerenciadorMensagens()

# ── Busca instalação ───────────────────────────────────────────────────────
def buscar_instalacao(nome_instalacao):
    if df.empty:
        return None, None
    
    busca = str(nome_instalacao).strip().lower()
    
    if "INSTALACAO" not in df.columns:
        return None, None

    coluna_limpa = df["INSTALACAO"].astype(str).str.replace(r'\.0$', '', regex=True).str.strip().str.lower()
    resultado = df[coluna_limpa == busca]
    
    if not resultado.empty:
        info = resultado.iloc[0]
        latitude  = info.get('LATITUDE_SAP', 'Não informada')
        longitude = info.get('LONGITUDE_SAP', 'Não informada')
        link_maps = f"https://www.google.com/maps?q={latitude},{longitude}"
        
        texto = (
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📋 *DADOS DA INSTALAÇÃO*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📍 Instalação: `{info.get('INSTALACAO', busca)}`\n"
            f"⏲️ Medidor: `{info.get('MEDIDOR', 'N/A')}`\n"
            f"📋 Classe: `{info.get('CLASSE_PRINCIPAL', 'N/A')}`\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🏠 *ENDEREÇO*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📬 Entrega: {info.get('CONTROLE_EXPEDICAO', 'N/A')}\n"
            f"🏷️ Complemento: {info.get('COMPLEMENTO', 'N/A')}\n"
            f"📌 Referência: {info.get('PONTO_REFERENCIA', 'N/A')}\n"
            f"🔢 Número: {info.get('NUMERO', 'N/A')}\n"
            f"━━━━━━━━━━━━━━━━━━━━"
        )
        return texto, link_maps
    return None, None

# ── Botões ─────────────────────────────────────────────────────────────────
def criar_botoes(link_maps):
    teclado = [
        [InlineKeyboardButton("🗺️ Ver no Mapa", url=link_maps)],
        [
            InlineKeyboardButton("🔄 Nova Consulta", callback_data="nova_consulta"),
            InlineKeyboardButton("📊 Status", callback_data="status")
        ]
    ]
    return InlineKeyboardMarkup(teclado)

# ── Comando /info ──────────────────────────────────────────────────────────
async def info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if context.args:
            nome = " ".join(context.args)
            texto, link_maps = buscar_instalacao(nome)
            if texto:
                botoes = criar_botoes(link_maps)
                aviso = "⚠️ *ATENÇÃO: NÃO ENTREGAR/CODIFICAR SEM CONFIRMAÇÃO DO LOCAL/MEDIDOR* ⚠️"
                msg1 = await gerenciador.enviar_mensagem_com_retry(
                    context.bot, update.effective_chat.id, texto,
                    parse_mode="Markdown", reply_markup=botoes
                )
                msg2 = await gerenciador.enviar_mensagem_com_retry(
                    context.bot, update.effective_chat.id, aviso, parse_mode="Markdown"
                )
                gerenciador.adicionar_mensagem(
                    update.effective_chat.id,
                    [update.message.message_id, msg1.message_id, msg2.message_id],
                    datetime.now()
                )
            else:
                msg = await gerenciador.enviar_mensagem_com_retry(
                    context.bot, update.effective_chat.id,
                    "❌ Instalação não encontrada. Verifique o número e tente novamente."
                )
                gerenciador.adicionar_mensagem(
                    update.effective_chat.id,
                    [update.message.message_id, msg.message_id],
                    datetime.now()
                )
        else:
            msg = await gerenciador.enviar_mensagem_com_retry(
                context.bot, update.effective_chat.id,
                "⚠️ Use: /info <numero_instalacao>\n\nExemplo: `/info 2001051823`",
                parse_mode="Markdown"
            )
            gerenciador.adicionar_mensagem(
                update.effective_chat.id,
                [update.message.message_id, msg.message_id],
                datetime.now()
            )
    except Exception as e:
        logger.error(f"❌ Erro no /info: {e}")
        try:
            texto_ajuda = (
                "❌ Comando inválido ou erro interno.\n\n"
                "👉 *Para pesquisar*, envie apenas o **número da instalação** no chat, ou use o comando:\n"
                "`/info NUMERO_DA_INSTALACAO`"
            )
            await update.message.reply_text(texto_ajuda, parse_mode="Markdown")
        except:
            pass

# ── Callback dos botões ────────────────────────────────────────────────────
async def botao_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "nova_consulta":
        msg = await gerenciador.enviar_mensagem_com_retry(
            context.bot, query.message.chat_id,
            "🔄 *Nova Consulta*\n\nDigite diretamente aqui no chat o **número da instalação** que deseja pesquisar:",
            parse_mode="Markdown"
        )
        gerenciador.adicionar_mensagem(
            query.message.chat_id, [msg.message_id], datetime.now()
        )

    elif query.data == "status":
        hoje = date.today()
        stats = gerenciador.obter_estatisticas()
        status_text = (
            f"📊 *Status do Bot*\n\n"
            f"📅 Consultas hoje ({hoje.strftime('%d/%m/%Y')}): {stats['hoje']}\n"
            f"📝 Total de consultas ativas na memória: {stats['total']}"
        )
        msg = await gerenciador.enviar_mensagem_com_retry(
            context.bot, query.message.chat_id, status_text, parse_mode="Markdown"
        )
        gerenciador.adicionar_mensagem(
            query.message.chat_id, [msg.message_id], datetime.now()
        )

# ── Captura textos soltos e números diretos ────────────────────────────────
async def mensagens_gerais(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto_recebido = update.message.text.strip()
    
    if texto_recebido.isdigit():
        context.args = [texto_recebido]
        await info(update, context)
    else:
        mensagem_ajuda = (
            "👋 Olá! Eu sou um bot de consulta rápida.\n\n"
            "👉 *Como fazer uma pesquisa:*\n"
            "Basta digitar o **número da instalação** diretamente aqui no chat (sem letras).\n\n"
            "Exemplo: `2001051823`"
        )
        msg = await gerenciador.enviar_mensagem_com_retry(
            context.bot, update.effective_chat.id, mensagem_ajuda, parse_mode="Markdown"
        )
        gerenciador.adicionar_mensagem(
            update.effective_chat.id,
            [update.message.message_id, msg.message_id],
            datetime.now()
        )

# ── Comando /status ────────────────────────────────────────────────────────
async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        hoje = date.today()
        stats = gerenciador.obter_estatisticas()
        status_text = (
            f"📊 *Status do Bot*\n\n"
            f"📅 Consultas hoje ({hoje.strftime('%d/%m/%Y')}): {stats['hoje']}\n"
            f"📝 Total de consultas ativas na memória: {stats['total']}\n"
        )
        await gerenciador.enviar_mensagem_com_retry(
            context.bot, update.effective_chat.id, status_text, parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"❌ Erro no /status: {e}")

# ── Comando /limpar ────────────────────────────────────────────────────────
async def limpar_agora(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await gerenciador.enviar_mensagem_com_retry(
            context.bot, update.effective_chat.id, "🧹 Iniciando limpeza da memória..."
        )
        await gerenciador.limpar_mensagens_teste(context.application)
        await gerenciador.enviar_mensagem_com_retry(
            context.bot, update.effective_chat.id, "✅ Memória limpa e mensagens apagadas!"
        )
    except Exception as e:
        logger.error(f"❌ Erro no /limpar: {e}")

# ── Inicialização ────────────────────────────────————————──────────────────
async def inicializar_bot(application):
    print("🤖 Bot de Consulta iniciado com leitura otimizada de múltiplas abas!")
    await gerenciador.limpar_mensagens_diarias(application)

# ── Main ───────────────────────────────────────────────────────────────────
def main():
    # INSIRA SEU TOKEN AQUI
    TOKEN = "8033688177:AAGbKMl6mDVrsTxF4VwDaWo_tc5ORRC8QII"

    request = HTTPXRequest(
        connection_pool_size=20,
        read_timeout=60.0,
        write_timeout=60.0,
        connect_timeout=30.0,
        pool_timeout=30.0,
    )

    app = Application.builder().token(TOKEN).request(request).post_init(inicializar_bot).build()

    app.add_handler(CommandHandler("info", info))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("limpar", limpar_agora))
    app.add_handler(CallbackQueryHandler(botao_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, mensagens_gerais))

    print("🚀 Iniciando bot...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()