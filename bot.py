# --------------------------------------------------------------------------------------
# Render Web Service対応 & Discord-Dropbox-Gemini連携 Bot (最終確定版)
# --------------------------------------------------------------------------------------
import os
import discord
from discord.ext import commands
import asyncio
import re
from datetime import datetime, timedelta
import dropbox
import json
import threading 
import requests
from flask import Flask, jsonify # Flaskからjsonifyもインポート
from waitress import serve # Flaskを本番環境で実行するための軽量サーバー

# --------------------------------------------------------------------------------------
# 環境変数の読み込み (Renderのシークレットを使用)
# --------------------------------------------------------------------------------------
DISCORD_TOKEN = os.environ.get('DISCORD_TOKEN', 'YOUR_DISCORD_TOKEN')
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY', 'YOUR_GEMINI_API_KEY')
DROPBOX_ACCESS_TOKEN = os.environ.get('DROPBOX_ACCESS_TOKEN', 'YOUR_DROPBOX_ACCESS_TOKEN')
DROPBOX_VAULT_ROOT = os.environ.get('DROPBOX_VAULT_ROOT', '/Obsidian Vault')

# Renderが提供するポートを使用
PORT = int(os.environ.get('PORT', 8080))

# --------------------------------------------------------------------------------------
# 設定
# --------------------------------------------------------------------------------------
# Discord Botの設定
intents = discord.Intents.default()
intents.message_content = True 
bot = commands.Bot(command_prefix='!', intents=intents)

# Flask Web Serviceの設定
app = Flask(__name__)

# Gemini API の設定
GEMINI_MODEL = "gemini-2.5-flash-preview-09-2025"
GEMINI_API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"

# Dropbox の設定
DBX_TIMEOUT = 10 

# --------------------------------------------------------------------------------------
# Web Service (Flask) の定義
# --------------------------------------------------------------------------------------

@app.route('/', methods=['GET'])
def home():
    """Renderのヘルスチェック用エンドポイント"""
    return jsonify({"status": "ok", "service": "Obsidian AI Bot Backend", "discord_status": "running"})

def run_web_server():
    """Waitressを使用してFlaskアプリを起動し、Renderのヘルスチェックに応答する"""
    print(f"Starting Waitress server on port {PORT}...")
    try:
        # RenderのPORT環境変数を使ってバインド
        serve(app, host='0.0.0.0', port=PORT)
    except Exception as e:
        print(f"Flask/Waitress server failed to start: {e}")

# --------------------------------------------------------------------------------------
# Dropbox 連携関数
# [変更なし]
# --------------------------------------------------------------------------------------

def _save_note_to_obsidian(file_path, content):
    """
    指定されたパスにMarkdownノートをDropbox経由で保存（または追記）する
    ファイルパスは「/VaultRoot/フォルダ名/ファイル名.md」形式
    """
    if not DROPBOX_ACCESS_TOKEN:
        print("ERROR: DROPBOX_ACCESS_TOKEN is not set.")
        return False, "DROPBOX_ACCESS_TOKEN が設定されていません。"

    try:
        dbx = dropbox.Dropbox(DROPBOX_ACCESS_TOKEN, timeout=DBX_TIMEOUT)
    except Exception as e:
        # トークン自体が無効、または接続エラーの場合
        print(f"Dropbox initialization error: {e}")
        return False, "Dropbox接続時にエラーが発生しました。トークンを確認してください。"
        
    try:
        # ファイルが存在するか確認
        metadata = dbx.files_get_metadata(file_path)
        
        # 既存ファイルがある場合：内容を読み込み、追記する
        if metadata:
            res, dbx_file = dbx.files_download(file_path)
            existing_content = dbx_file.content.decode('utf-8')
            
            # 追記の区切りとしてタイムスタンプを挿入
            now_jst = datetime.now() + timedelta(hours=9)
            divider = f"\n\n---\n\n## 📝 追記: {now_jst.strftime('%Y-%m-%d %H:%M:%S')}\n"
            new_content = existing_content + divider + content
            
            # ファイルを上書きアップロード
            dbx.files_upload(new_content.encode('utf-8'), file_path, 
                             mode=dropbox.files.WriteMode('overwrite'))
            return True, "追記"

    except dropbox.exceptions.ApiError as err:
        # ファイルが存在しない場合 (エラーコード e.path_lookup.not_found)
        if isinstance(err.error, dropbox.files.GetMetadataError) and err.error.get_path().is_not_found():
            # 新規作成として処理
            pass
        elif err.error.is_path() and err.error.get_path().is_insufficient_permissions():
            return False, "Dropboxのアクセス権限が不足しています。トークン権限を確認してください。"
        else:
            # その他のAPIエラー（例: 無効なトークンなど）
            print(f"Dropbox API Error: {err}")
            return False, "Dropbox連携エラー！\n原因: DROPBOX_ACCESS_TOKEN が無効です。\n対処法: ステップ1に戻り、トークンを再取得してRenderの環境変数に設定してください。"
            
    except Exception as e:
        print(f"General Dropbox Error: {e}")
        return False, "Dropbox接続時に不明なエラーが発生しました。"


    # 新規作成の処理
    try:
        dbx.files_upload(content.encode('utf-8'), file_path, 
                         mode=dropbox.files.WriteMode('add'))
        return True, "新規保存"
    except Exception as e:
        print(f"Dropbox Upload Error: {e}")
        return False, "Dropboxファイルアップロードに失敗しました。"

# --------------------------------------------------------------------------------------
# Gemini API 連携関数
# [変更なし]
# --------------------------------------------------------------------------------------

async def _call_gemini_api(prompt, content):
    """
    Gemini APIを呼び出し、要約、タイトル、フォルダ名、タグを取得する
    """
    if not GEMINI_API_KEY:
        return "ERROR: GEMINI_API_KEY is not set."

    # システムプロンプトを会話ログから取得する形式に変更
    system_instruction = (
        "あなたはDiscordでの会話内容を、Obsidian Vaultに保存するプロフェッショナルなAIアシスタントです。 "
        "ユーザーがBotにメンションした直前のメッセージを受け取ります。"
        "以下のルールに従って、会話の内容を要約し、保存内容をMarkdown形式で構造化してください。"
        "**出力はJSON形式のみとし、Markdownテキストや説明文は一切含めないでください。**"
        
        "1. **Markdown Text (text)**: Botにメンションしたユーザーメッセージの内容を500文字以内で要約し、Markdown形式で整形します。Obsidianの[[]]リンクや`#タグ`を含めます。"
        "2. **Estimated Title (title)**: ノートファイルのタイトルを提案します。例: `2025-01-01 定例会議議事録`"
        "3. **Target Folder (folder)**: ノートを保存するObsidian Vault内のフォルダ名を提案します。例: `Inbox`、`ProjectX`、`雑談`。指定がない場合は `Discord Inbox` とします。"
    )

    user_query = f"Discordで受け取ったメッセージは以下の通りです。このメッセージをObsidian Vaultに保存するための要約、タイトル、保存先フォルダ名を提案してください。\n\nメッセージ内容: \"{content}\""

    payload = {
        "contents": [{"parts": [{"text": user_query}]}],
        "systemInstruction": {"parts": [{"text": system_instruction}]},
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": {
                "type": "OBJECT",
                "properties": {
                    "text": {"type": "STRING", "description": "Markdown形式の要約とコンテンツ"},
                    "title": {"type": "STRING", "description": "ノートファイルのタイトル"},
                    "folder": {"type": "STRING", "description": "保存先のフォルダ名"}
                },
                "required": ["text", "title", "folder"]
            }
        },
    }

    try:
        response = requests.post(
            GEMINI_API_URL, 
            headers={'Content-Type': 'application/json'}, 
            data=json.dumps(payload), 
            timeout=30 # タイムアウト設定
        )
        response.raise_for_status() # HTTPエラーが発生した場合に例外を発生させる
        
        result = response.json()
        
        # JSON文字列を解析
        json_text = result['candidates'][0]['content']['parts'][0]['text']
        parsed_json = json.loads(json_text)
        
        return parsed_json
        
    except requests.exceptions.Timeout:
        return {"error": "APIリクエストがタイムアウトしました。"}
    except requests.exceptions.RequestException as e:
        print(f"API Request Error: {e}")
        return {"error": f"APIリクエストエラー: {e}"}
    except (KeyError, json.JSONDecodeError) as e:
        print(f"API Response Parsing Error: {e} | Raw Response: {response.text}")
        return {"error": "AIからの応答解析中にエラーが発生しました。"}


# --------------------------------------------------------------------------------------
# Discord Bot イベントとコマンド
# [変更なし]
# --------------------------------------------------------------------------------------

@bot.event
async def on_ready():
    """Botがログインして準備ができたときに実行される"""
    print(f'Bot がログインしました: {bot.user.name} (ID: {bot.user.id})')
    # Flaskサーバーが起動していることを確認するためのメッセージ
    print("Bot is running and ready for Discord communication.")
    if GEMINI_API_KEY:
        print("✅ AI連携機能が有効です。")
    else:
        print("❌ WARNING: GEMINI_API_KEYが設定されていません。AI機能は利用できません。")

@bot.event
async def on_message(message):
    """メッセージを受信したときに実行される"""
    # Bot自身のメッセージは無視
    if message.author == bot.user:
        return

    # Botへのメンションがあり、かつメッセージが 'メモ' で終わる場合
    if bot.user.mentioned_in(message) and message.content.strip().lower().endswith('メモ'):
        
        # メンション部分を除去して純粋な内容を取得
        content = re.sub(r'<@!?\d+>', '', message.content).strip()
        content = content.removesuffix('メモ').strip()

        if not content:
            await message.channel.send("メモの対象となるメッセージの内容がありません。")
            return

        # 処理中のリアクションを追加
        await message.add_reaction('⏳')

        # AI処理の実行
        gemini_response = await _call_gemini_api(message.content, content)
        
        # 処理中のリアクションを削除
        await message.remove_reaction('⏳', bot.user)

        if "error" in gemini_response:
            await message.channel.send(f"❌ AI処理エラー: {gemini_response['error']}")
            return

        # AIの提案内容を変数に格納
        suggested_title = gemini_response.get("title", "無題のメモ")
        suggested_folder = gemini_response.get("folder", "Discord Inbox")
        suggested_text = gemini_response.get("text", "要約できませんでした。")

        # ファイルパスの整形
        # ファイル名に使えない文字を置換または削除
        clean_title = suggested_title.replace('/', '_').replace('\\', '_').strip()
        clean_folder = suggested_folder.replace('/', '_').replace('\\', '_').strip()
        
        # 最終的なファイルパス
        final_file_path = os.path.join(DROPBOX_VAULT_ROOT, clean_folder, f"{clean_title}.md")
        # Dropbox APIはパスの区切りに '/' を使うため置換
        final_file_path = final_file_path.replace('\\', '/')

        # Discordのタイムスタンプとリンク
        timestamp_link = f"[Discord メッセージへ]({message.jump_url})"
        
        # 最終的に保存する内容を組み立て
        note_content = (
            f"--- Discord メモ ---\n"
            f"作成日時:: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
            f"ユーザー:: {message.author.name}\n"
            f"チャンネル:: #{message.channel.name}\n"
            f"メッセージ:: {timestamp_link}\n"
            f"元のメッセージ内容:\n"
            f"```\n{content}\n```\n\n"
            f"--- AI要約・提案内容 ---\n"
            f"{suggested_text}\n"
        )
        
        # ユーザーに確認メッセージを送信
        preview_message = await message.channel.send(
            f"**AI要約のプレビュー**\n"
            f"推定タイトル: `{suggested_title}` (フォルダ: `{suggested_folder}`)\n\n"
            f"--- AI提案内容 (500文字まで) ---\n"
            f"```markdown\n{suggested_text}\n```\n" # Markdown形式で表示
            f"\n\n**この内容をObsidian Vaultに保存しますか？**\n"
            f"✅: 保存（既存ノートがあれば追記） / ❌: キャンセル"
        )
        
        await preview_message.add_reaction('✅')
        await preview_message.add_reaction('❌')

        def check(reaction, user):
            return user == message.author and str(reaction.emoji) in ['✅', '❌'] and reaction.message.id == preview_message.id

        try:
            reaction, user = await bot.wait_for('reaction_add', timeout=60.0, check=check)

            if str(reaction.emoji) == '✅':
                # 保存実行
                success, save_message = await asyncio.to_thread(
                    _save_note_to_obsidian, final_file_path, note_content
                )
                
                if success:
                    action_type = "新規保存" if "新規保存" in save_message else "追記"
                    final_reply = (
                        f"✅ ノートをObsidian Vaultに**{action_type}**しました。\n"
                        f"**タイトル:** `{suggested_title}`\n"
                        f"**保存先:** `{final_file_path}`"
                    )
                else:
                    final_reply = f"❌ ファイル保存に失敗しました。\n\n詳細: {save_message}"
                
                await preview_message.edit(content=final_reply)
                await preview_message.clear_reactions()

            else: # ❌でキャンセルされた場合
                await preview_message.edit(content="❌ ノートの保存をキャンセルしました。")
                await preview_message.clear_reactions()

        except asyncio.TimeoutError:
            await preview_message.edit(content="⚠️ 60秒間リアクションがなかったため、ノートの保存をキャンセルしました。")
            await preview_message.clear_reactions()
        except Exception as e:
            print(f"Reaction/Save Error: {e}")
            await preview_message.edit(content=f"🚨 予期せぬエラーが発生しました: {e}")
            await preview_message.clear_reactions()
            

# --------------------------------------------------------------------------------------
# Botの起動ロジック (メイン実行)
# --------------------------------------------------------------------------------------

# NOTE: RenderのWeb Serviceとして、Flaskサーバー起動とDiscord Bot起動を並行実行する

if __name__ == '__main__':
    if not DISCORD_TOKEN or not GEMINI_API_KEY or not DROPBOX_ACCESS_TOKEN:
        print("--- 🚨 ERROR: 必要な環境変数が設定されていません。 ---")
        print("DISCORD_TOKEN, GEMINI_API_KEY, DROPBOX_ACCESS_TOKEN の3つを設定してください。")
    else:
        # 1. Webサーバーを別スレッドで起動 (Renderのヘルスチェック用)
        web_server_thread = threading.Thread(target=run_web_server)
        web_server_thread.daemon = True 
        web_server_thread.start()
        
        # 2. Discord Botをメインスレッドで起動
        try:
            bot.run(DISCORD_TOKEN)
        except discord.LoginFailure:
            print("--- 🚨 ERROR: DISCORD_TOKEN が不正です。 ---")
        except Exception as e:
            print(f"--- 🚨 ERROR: 予期せぬエラーが発生しました: {e} ---")