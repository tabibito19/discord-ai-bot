import os
import threading
from discord.ext import commands
from discord import Intents
from flask import Flask
from waitress import serve

# --- Webサーバー（Flask + Waitress）の設定 ---
# Discord Botの起動を邪魔しないよう、別のスレッドで実行します。
# これにより、Renderがポートを検出し、タイムアウトを防ぎます。

app = Flask(__name__)
PORT = int(os.environ.get('PORT', 5000)) # Renderの環境変数からポートを取得

@app.route('/')
def home():
    # Renderのヘルスチェック用エンドポイント
    return "Discord Bot is running and the web server is operational!"

def start_server():
    """Waitressを使ってWebサーバーを起動する関数"""
    print(f"Starting Waitress server on port {PORT}...")
    try:
        # host='0.0.0.0'はRender上で必須です
        serve(app, host='0.0.0.0', port=PORT)
    except Exception as e:
        print(f"Web server failed to start: {e}")

# --- Discord Botの設定 ---

# 環境変数からトークンを取得 (Renderの設定で登録しているはずです)
DISCORD_TOKEN = os.environ.get('DISCORD_TOKEN')

# 必要なIntents（権限）を設定
intents = Intents.default()
intents.message_content = True # メッセージの内容を読むために必要
client = commands.Bot(command_prefix='!', intents=intents)

@client.event
async def on_ready():
    """BotがDiscordに接続したときに実行されます"""
    print(f'Bot がログインしました: {client.user}')

@client.event
async def on_message(message):
    """メッセージを受信したときに実行されます"""
    # Bot自身のメッセージは無視
    if message.author == client.user:
        return

    # 'hello' というメッセージに反応する例
    if message.content.lower().startswith('hello'):
        await message.channel.send(f'こんにちは、{message.author.display_name}さん！')
    
    # commands.Botを使っているため、コマンド処理も必要
    await client.process_commands(message)

# --- メイン処理 ---
if __name__ == '__main__':
    # 1. Webサーバーを別スレッドで起動
    server_thread = threading.Thread(target=start_server)
    # スレッドをデーモンとして設定（メインプログラム終了時に一緒に終了させる）
    server_thread.daemon = True 
    server_thread.start()

    # 2. Discord Botをメインスレッドで起動（ブロッキング処理）
    if DISCORD_TOKEN:
        print("Starting Discord Bot...")
        try:
            client.run(DISCORD_TOKEN)
        except Exception as e:
            print(f"Discord Bot failed to run: {e}")
    else:
        print("Error: DISCORD_TOKEN environment variable is not set.")