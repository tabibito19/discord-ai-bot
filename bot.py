import discord
from discord import app_commands
from dotenv import load_dotenv
import os
import aiohttp
import asyncio
import dropbox
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading

# 環境変数を読み込む
# Renderでは環境変数は自動でロードされるため不要だが、ローカル実行用に残しておく
load_dotenv()

# 環境変数からトークンを取得 (Renderデプロイ時に使用)
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
DROPBOX_ACCESS_TOKEN = os.getenv('DROPBOX_ACCESS_TOKEN')
# Render Web Serviceがリッスンすべきポートを取得
PORT = int(os.environ.get('PORT', 10000))

# Gemini APIのエンドポイント
GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-preview-09-2025:generateContent"
# APIキーはURLパラメータとして追加する

# --- 1. RenderのポートスキャンをクリアするためのダミーWebサーバー ---

# RenderのWeb Serviceとして認識させるため、ポートを開くためのダミーサーバー
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        # 正常な応答を返す（ヘルスチェック用）
        self.send_response(200)
        self.send_header('Content-type', 'text/html')
        self.end_headers()
        self.wfile.write(b"Bot is awake and running.")

def run_web_server():
    """Webサーバーを別スレッドで起動する"""
    server_address = ('0.0.0.0', PORT)
    httpd = HTTPServer(server_address, HealthCheckHandler)
    print(f"--- Render Health Check: Dummy Web Server running on port {PORT} ---")
    httpd.serve_forever()

# --- 2. Discord Bot本体 ---

class ObsidianBot(discord.Client):
    def __init__(self):
        # Intents: Botが必要とするイベントの種類を指定
        intents = discord.Intents.default()
        intents.messages = True
        intents.message_content = True
        super().__init__(intents=intents)
        # コマンドツリーの準備
        self.tree = app_commands.CommandTree(self)

    async def on_ready(self):
        """BotがDiscordに接続したときに実行される"""
        print(f'Bot is ready and connected to Discord! Logged in as {self.user}')
        await self.tree.sync() # スラッシュコマンドをDiscordに登録
        print(f"Synced {len(self.tree.get_commands())} command(s).")
        print("-" * 30)

    # --- 3. Gemini API呼び出し関数 ---

    async def generate_note_title_and_content(self, user_prompt: str) -> tuple[str, str]:
        """Gemini APIを呼び出し、タイトルとMarkdownコンテンツを生成する"""
        
        # システムプロンプト: AIの役割と出力形式を定義
        system_prompt = (
            "あなたはユーザーがObsidianに素早くメモを取るためのプロフェッショナルなAIアシスタントです。"
            "提供されたメモの内容を分析し、以下の要件を満たすObsidian形式のMarkdownファイルの内容を生成してください。"
            "1. 応答は必ずJSON形式であること。"
            "2. JSONには `title` (日本語、簡潔、30文字以内) と `markdown_content` (Obsidianで読みやすいMarkdown形式のメモ本文) の2つのキーを含めること。"
            "3. メモ本文は、入力内容を整理し、以下の構造でMarkdownの箇条書きやヘッダーを使って記述してください。"
            "   - **# 概要**: メモ全体の要点を簡潔にまとめる。"
            "   - **## 主要なアイデア**: 箇条書きでアイデアを詳述する。"
            "   - **## 次のアクション**: 具体的かつ実行可能な次のステップやタスクリストを記述する。"
            "4. 引用符などの余計な文字列を含めず、純粋なJSONテキストのみを応答の最初に記述すること。"
        )
        
        # APIリクエストのペイロード
        payload = {
            "contents": [{"parts": [{"text": f"以下のメモを整理してMarkdownとタイトルを生成してください: {user_prompt}"}]}],
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": {
                    "type": "OBJECT",
                    "properties": {
                        "title": {"type": "STRING", "description": "Markdownファイルのタイトル。日本語、簡潔、30文字以内。"},
                        "markdown_content": {"type": "STRING", "description": "Obsidian形式のMarkdownコンテンツ。ヘッダーと箇条書きを使用。"}
                    },
                    "propertyOrdering": ["title", "markdown_content"]
                }
            }
        }

        # APIキーをURLに含める
        url = f"{GEMINI_API_URL}?key={GEMINI_API_KEY}"

        async with aiohttp.ClientSession() as session:
            try:
                # APIコールを実行
                async with session.post(url, json=payload, ssl=True) as response:
                    if response.status != 200:
                        print(f"Gemini API Error: HTTP Status {response.status}")
                        return "APIエラー", f"Gemini APIから応答がありませんでした。ステータスコード: {response.status}"

                    result = await response.json()
                    
                    # 応答の解析
                    json_text = result.get('candidates', [{}])[0].get('content', {}).get('parts', [{}])[0].get('text')
                    
                    if not json_text:
                        print("Gemini API Error: Response content is empty.")
                        return "API応答エラー", "Geminiから空の応答が返されました。"
                        
                    # JSON文字列からタイトルとコンテンツを抽出
                    try:
                        parsed_json = json.loads(json_text)
                        title = parsed_json.get('title', '無題のメモ')
                        content = parsed_json.get('markdown_content', 'コンテンツの生成に失敗しました。')
                        return title, content
                    except json.JSONDecodeError:
                        print(f"Gemini API Error: Invalid JSON response: {json_text}")
                        return "JSON解析エラー", f"Geminiからの応答が不正です: {json_text[:100]}..."

            except Exception as e:
                print(f"An error occurred during Gemini API call: {e}")
                return "通信エラー", f"Gemini APIとの通信中に予期せぬエラーが発生しました: {e}"

    # --- 4. Dropboxアップロード関数 ---

    def upload_to_dropbox(self, filename: str, content: str):
        """MarkdownファイルをDropboxの指定フォルダにアップロードする"""
        try:
            dbx = dropbox.Dropbox(DROPBOX_ACCESS_TOKEN)
            # Dropbox上のパス。ここでは /Obsidian_Notes フォルダに保存
            dropbox_path = f"/Obsidian_Notes/{filename}" 
            
            # ファイルのアップロード（上書きモード）
            dbx.files_upload(content.encode('utf-8'), dropbox_path, mode=dropbox.files.WriteMode('overwrite'))
            
            return True, f"✅ Dropboxにアップロード完了:\n`{dropbox_path}`"
        except Exception as e:
            print(f"Dropbox Error: {e}")
            return False, f"❌ Dropboxへのアップロードに失敗しました。\nエラー詳細: `{e}`"

    # --- 5. Discordスラッシュコマンドの定義 ---

    @app_commands.command(name="note", description="Gemini AIを使ってメモを整理し、DropboxのObsidianフォルダに保存します。")
    @app_commands.describe(memo_content="Obsidianに残したいアイデアやメモを入力してください。")
    async def create_note(self, interaction: discord.Interaction, memo_content: str):
        """/note コマンドの実行処理"""
        
        # 遅延応答（Botがすぐに反応するための処理）
        await interaction.response.send_message(
            f"🖊️ メモ内容: `{memo_content[:50]}...`\n\n**AIが内容を整理し、Dropboxへのアップロード準備中です...**\n（約10〜20秒かかります）",
            ephemeral=True # 他のユーザーには見えないようにする
        )
        
        # 1. AIによる生成
        try:
            title, markdown_content = await self.generate_note_title_and_content(memo_content)
        except Exception as e:
            await interaction.followup.send(f"❌ AI生成中に予期せぬエラーが発生しました: {e}", ephemeral=True)
            return
            
        # 2. ファイル名とコンテンツの整形
        # ファイル名を安全な形式に整形（日付とタイトル）
        timestamp = discord.utils.utcnow().strftime("%Y-%m-%d-%H%M")
        safe_title = "".join(c for c in title if c.isalnum() or c in (' ', '-', '_')).rstrip()
        filename = f"{timestamp}_{safe_title}.md"
        
        # 3. Dropboxへのアップロード
        # 注意: dropboxのライブラリは非同期ではないため、別スレッドで実行する
        upload_success, upload_message = await self.loop.run_in_executor(
            None, self.upload_to_dropbox, filename, markdown_content
        )

        # 4. 結果の応答
        if upload_success:
            embed = discord.Embed(
                title=f"📝 {title}",
                description="**Obsidian用メモの作成とアップロードが完了しました！**",
                color=discord.Color.green()
            )
            embed.add_field(name="アップロード先", value=upload_message, inline=False)
            embed.add_field(name="メモのプレビュー", value=f"