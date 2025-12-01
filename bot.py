# -------------------------------------------------------------
# Discord Bot - Obsidian連携 AI Bot (Render + Dropbox対応版)
# -------------------------------------------------------------
import discord
from discord.ext import commands
import os
import json
import asyncio
import re
import aiohttp
from datetime import datetime
import requests
import dropbox # Dropbox SDKをインポート

# ===============================================
# 🔑 環境変数からの設定読み込み (Renderでの実行に必須)
# ===============================================
# これらの値は、Renderの環境変数として設定します。
TOKEN = os.environ.get("DISCORD_TOKEN", "YOUR_BOT_TOKEN_HERE") 
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "AIzaSyB3-EXAMPLE-KEY-FOR-GEMINI") 
DROPBOX_ACCESS_TOKEN = os.environ.get("DROPBOX_ACCESS_TOKEN", "YOUR_DROPBOX_TOKEN_HERE")
DROPBOX_VAULT_ROOT = os.environ.get("DROPBOX_VAULT_ROOT", "/Obsidian Vault") # 例: /Obsidian Vault

# 🔧 その他の設定
GEMINI_MODEL = "gemini-2.5-flash-preview-09-2025" 
NOTE_FOLDER = "Discord Inbox" 
QUICK_CAPTURE_CHANNEL_NAME = "quick-capture" # クイックキャプチャを有効にしたいチャンネル名

# -------------------------------------------------------------
# Botの基本設定
# -------------------------------------------------------------
intents = discord.Intents.default()
# 必須のインテントをすべて有効化
intents.message_content = True 
intents.messages = True 
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)

# -------------------------------------------------------------
# Utility Function: Dropboxへのファイル書き込みロジック (E機能含む)
# -------------------------------------------------------------
def _save_note_to_obsidian(message, title_text, dynamic_folder, content_for_reply):
    """
    Dropbox API経由でObsidianへのノート保存または追記を実行する。
    """
    if not DROPBOX_ACCESS_TOKEN:
        raise Exception("DROPBOX_ACCESS_TOKEN が設定されていません。")

    dbx = dropbox.Dropbox(DROPBOX_ACCESS_TOKEN)
    
    # 1. ファイル名の決定とパスの構築
    safe_title = re.sub(r'[\\/:*?"<>|#\[\]]', '', title_text).strip()
    if not safe_title:
        safe_title = f"Discord Memo {datetime.now().strftime('%Y%m%d%H%M%S')}"

    # Dropbox上の絶対パスを構築 (Vault Root/Dynamic Folder/Filename.md)
    # 例: /Obsidian Vault/Ideas/My New Title.md
    dropbox_dir_path = os.path.join(DROPBOX_VAULT_ROOT, dynamic_folder).replace('\\', '/')
    dropbox_file_path = os.path.join(dropbox_dir_path, f"{safe_title}.md").replace('\\', '/')

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    metadata = f"---\nchannel: {message.channel.name}\nauthor: {message.author.name}\ntimestamp: {timestamp}\n---\n"
    
    is_appending = False
    existing_content = ""

    # 2. 💡 拡張機能 E: 既存ノートの存在チェックと内容の取得
    try:
        # ファイルのメタデータを取得 (存在チェック)
        metadata_result = dbx.files_get_metadata(dropbox_file_path)
        is_appending = True
        
        # 既存ファイルのダウンロード
        _, response = dbx.files_download(dropbox_file_path)
        existing_content = response.content.decode('utf-8')
        
    except dropbox.exceptions.ApiError as err:
        if err.error.is_path() and err.error.get_path().is_not_found():
            # ファイルが存在しない場合は新規作成
            is_appending = False
        else:
            raise err

    # 3. ファイルの内容作成とアップロード
    if is_appending:
        # 既存ファイルがある場合は追記
        save_action = "追記"
        
        # 追記する内容 (セクションタイトルとタイムスタンプを追加)
        append_content = (
            f"\n\n---\n\n## 追記: {timestamp} (by {message.author.name})\n\n"
            f"{content_for_reply}"
        )
        final_content = existing_content + append_content
        
        # Dropboxへのアップロード (Overwriteモード)
        dbx.files_upload(
            final_content.encode('utf-8'),
            dropbox_file_path,
            mode=dropbox.files.WriteMode.overwrite
        )
    else:
        # 新規ファイルの場合は作成
        save_action = "保存"
        final_content = f"{metadata}\n# {title_text}\n\n{content_for_reply}"
        
        # Dropboxへのアップロード
        dbx.files_upload(
            final_content.encode('utf-8'),
            dropbox_file_path,
            mode=dropbox.files.WriteMode.add
        )
    
    # Dropboxパスを返却
    save_path = dropbox_file_path
    
    return save_action, save_path, title_text, timestamp

# -------------------------------------------------------------
# Utility Function: Gemini APIと確認ステップのコアロジック
# -------------------------------------------------------------
async def _process_message_with_ai(message, cleaned_content):
    """
    AI処理を行い、確認ステップを経て、Obsidianに保存します。
    """
    # AIへの指示プロンプト
    prompt = f"このDiscordメッセージをObsidianノート用の要約としてください。タイトルはメッセージの内容に基づき日本語で3語以内とし、内容は箇条書きにまとめてください。また、メッセージの内容に基づいて関連するキーワードを3つ以上抽出し、Obsidianのタグ（#）形式で文末に追加してください。関連する既存のノート名がある場合は、Obsidianの内部リンク形式（例：[[既存ノート名]]）で内容に含めてください。**メッセージの内容に基づき、最適なカテゴリ名（例: '技術', 'アイデア', '買い物', '雑談' など）を一つ選び、Markdownのフロントマターの 'folder:' フィールドに出力してください。**\n\nメッセージ: {cleaned_content}"

    # 1. Gemini API呼び出し
    try:
        async with aiohttp.ClientSession() as session:
            system_prompt = "あなたは、ユーザーからのDiscordメッセージを、Obsidianに保存するのに最適なMarkdown形式のノートに変換するAIアシスタントです。余計な説明や挨拶はせず、タイトルと内容のMarkdownのみを出力してください。"
            
            # 🚨 警告解消のため、API URLのモデル名を更新
            api_url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
            
            payload = {
                "contents": [{"parts": [{"text": prompt}]}],
                "systemInstruction": {"parts": [{"text": system_prompt}]},
                "generationConfig": {"temperature": 0.2} 
            }

            async with session.post(api_url, json=payload, timeout=30) as response:
                if response.status != 200:
                    error_text = await response.text()
                    raise Exception(f"API Error {response.status}: {error_text}")

                result = await response.json()
                
                try:
                    generated_text = result['candidates'][0]['content']['parts'][0]['text']
                except (IndexError, KeyError):
                    print(f"🚨 API応答解析エラー: 期待されるテキストが見つかりませんでした。完全な応答: {json.dumps(result, indent=2)}")
                    raise Exception("AIからの応答構造が無効です。メッセージが安全ポリシーに違反した可能性があります。")
                
                if not generated_text:
                    raise Exception("AIが空の応答を返しました。")
        
        # 2. AI応答からのデータ抽出
        dynamic_folder = NOTE_FOLDER
        
        # AI応答から folder: フィールドを正規表現で抽出
        folder_match = re.search(r"^\s*folder:\s*(.+)$", generated_text, re.MULTILINE | re.IGNORECASE)

        if folder_match:
            extracted_folder = folder_match.group(1).strip().strip("'\"")
            # フォルダ名として安全な文字のみを許可
            if extracted_folder and re.match(r'^[\w\s\-\.\/]+$', extracted_folder):
                dynamic_folder = extracted_folder
                
        # 生成されたテキストからタイトルを抽出
        match = re.search(r"^#\s*(.+)", generated_text, re.MULTILINE)
        if match:
            title_text = match.group(1).strip()
        else:
            # タイトルが見つからなかった場合のフォールバック
            title_text = cleaned_content[:15].replace('\n', ' ').strip()
            
        # フロントマターを除いて内容を取得
        content_without_frontmatter = re.sub(r"---.*?---", "", generated_text, flags=re.DOTALL).strip()
        
        # 最初のMarkdownヘッダー（タイトル）を削除し、内容のみを取得
        content_for_reply = re.sub(r"^#\s*.+\n*", "", content_without_frontmatter, count=1, flags=re.MULTILINE).strip()
        
        # 3. 💡 確認ステップの開始
        content_preview = content_for_reply[:500]
        
        confirm_message_text = f"""\
**AI要約のプレビュー**
**推定タイトル:** `{title_text}` (フォルダ: `{dynamic_folder}`)

--- AI提案内容 (500文字まで) ---
```markdown
{content_preview}...\
```

**この内容をObsidian Vaultに保存しますか？**
✅: 保存（既存ノートがあれば追記） / ❌: キャンセル"""
        
        # 確認メッセージを送信し、リアクションを待つ
        confirm_msg = await message.reply(confirm_message_text)
        await confirm_msg.add_reaction('✅') # 承認リアクション
        await confirm_msg.add_reaction('❌') # キャンセルリアクション

        # リアクションチェック関数
        def check(reaction, user):
            return user == message.author and str(reaction.emoji) in ['✅', '❌'] and reaction.message.id == confirm_msg.id

        try:
            # 60秒間リアクションを待つ
            reaction, user = await bot.wait_for('reaction_add', timeout=60.0, check=check)

            if str(reaction.emoji) == '✅':
                # 4. 保存実行
                # NOTE: ここで Dropbox API連携の関数が実行される
                save_action, save_path, final_title, timestamp = _save_note_to_obsidian(
                    message, title_text, dynamic_folder, content_for_reply
                )

                # 5. 完了通知
                final_reply = (
                    f"✅ ノートをObsidian Vaultに**{save_action}**しました。\n"
                    f"**タイトル:** `{final_title}`\n"
                    f"**保存先 (Dropbox):** `{save_path}`\n"
                    f"**日時:** `{timestamp}`"
                )
                
                try:
                    await confirm_msg.edit(content=final_reply)
                    await confirm_msg.clear_reactions() 
                except discord.errors.NotFound:
                    print("⚠️ 完了通知メッセージはユーザーによって削除されました。")
                except Exception as edit_e:
                    print(f"🚨 完了通知メッセージ編集中にエラーが発生しました: {edit_e}")


            else: # ❌でキャンセルされた場合
                try:
                    await confirm_msg.edit(content="❌ ノートの保存をキャンセルしました。")
                    await confirm_msg.clear_reactions()
                except discord.errors.NotFound:
                    print("⚠️ キャンセル通知メッセージはユーザーによって削除されました。")
                except Exception as edit_e:
                    print(f"🚨 キャンセル通知メッセージ編集中にエラーが発生しました: {edit_e}")

        except asyncio.TimeoutError:
            # 6. タイムアウト処理
            try:
                await confirm_msg.edit(content="⚠️ 60秒間リアクションがなかったため、ノートの保存をキャンセルしました。")
                await confirm_msg.clear_reactions()
            except discord.errors.NotFound:
                print("⚠️ タイムアウト通知メッセージはユーザーによって削除されました。")
            except Exception as edit_e:
                print(f"🚨 タイムアウト通知メッセージ編集中にエラーが発生しました: {edit_e}")


    except asyncio.TimeoutError:
        error_message = f"🚨 API呼び出しがタイムアウトしました (30秒)。ネットワークまたはAPIキーを再確認してください。"
        print(f"エラーログ: {error_message}")
        await message.reply(f"🚨 APIタイムアウト！\n**原因:** 処理が完了しませんでした。ネットワークまたはAPIキーを確認してください。")

    except dropbox.exceptions.AuthError:
        # Dropbox認証エラーをキャッチ
        error_message = f"🚨 Dropbox認証エラー: アクセストークンを確認してください。"
        print(f"エラーログ: {error_message}")
        await message.reply(f"🚨 **Dropbox連携エラー！**\n**原因:** `DROPBOX_ACCESS_TOKEN` が無効です。\n**対処法:** ステップ1に戻り、トークンを再取得してRenderの環境変数に設定してください。")
        
    except Exception as e:
        error_message = f"AI処理中またはファイル保存中に予期せぬエラーが発生しました。エラー詳細: {e}"
        print(f"エラーログ: {error_message}")
        if "Missing Permissions" in str(e):
             await message.reply(
                 f"🚨 **致命的なエラー: Discord権限不足 (50013)**\n\n"
                 f"**原因:** BotがDiscordメッセージの編集やリアクションの操作に必要な権限を持っていません。\n"
                 f"**対処法:** ステップ2に戻り、Botに以下の権限があるか確認してください。\n"
                 f"1. **リアクションを追加**\n"
                 f"2. **メッセージを管理** (編集・リアクションクリアに必要)\n"
                 f"3. **メッセージを送信**"
             )
        else:
             await message.reply(f"エラーによりノートの作成に失敗しました。\n詳細をコンソールで確認してください。")


# -------------------------------------------------------------
# Botの起動ロジック (設定チェックと実行)
# -------------------------------------------------------------
# Render環境では環境変数として設定されるため、ローカルでのテスト目的でのみチェック
if TOKEN == "YOUR_BOT_TOKEN_HERE" or \
   DROPBOX_ACCESS_TOKEN == "YOUR_DROPBOX_TOKEN_HERE" or \
   GEMINI_API_KEY == "AIzaSyB3-EXAMPLE-KEY-FOR-GEMINI":
    
    print("\n\n")
    print("=== 設定エラーが発生しました (ローカルテスト用) ===")
    print("本番環境(Render)では環境変数で設定されますが、ローカルでのテストのためにチェックしています。")
    print("以下のうち、どれか一つがプレースホルダーのままです。")
    print(" -> 1. DISCORD_TOKEN")
    print(" -> 2. DROPBOX_ACCESS_TOKEN")
    print(" -> 3. GEMINI_API_KEY")
    print("=========================================")
    print("\n\n")

else:
    # 全ての設定が完了している場合のみ、Botを起動
    @bot.event
    async def on_ready():
        print(f"Bot がログインしました: {bot.user} (ID: {bot.user.id})")
        print("\n=======================================================")
        print("✅ AI連携機能が有効です。")
        print(f"   - Dropbox Vault Root: {DROPBOX_VAULT_ROOT}")
        print(f"   - クイックキャプチャチャンネル: #{QUICK_CAPTURE_CHANNEL_NAME}")
        print("=======================================================\n")

    # メッセージ処理ロジック (AI連携含む)
    @bot.event
    async def on_message(message):
        if message.author.bot or message.webhook_id:
            return

        is_quick_capture = message.channel.name == QUICK_CAPTURE_CHANNEL_NAME

        # 処理トリガーの判定（メッセージがBot自身へのメンションか、クイックキャプチャチャンネルでのメッセージか）
        is_triggered = bot.user.mentioned_in(message) or is_quick_capture
        
        if not is_triggered:
            return

        await message.channel.typing()
        
        cleaned_content = message.content

        # メンション削除 (クイックキャプチャチャンネル以外でのメンション時)
        if bot.user.mentioned_in(message):
            # メンションをメッセージから削除
            cleaned_content = cleaned_content.replace(f"<@{bot.user.id}>", "", 1).strip()
            cleaned_content = cleaned_content.replace(f"@{bot.user.name}", "", 1).strip()
            
        # 添付ファイルがある場合、画像処理は行わないが、テキストコンテンツがない場合はエラー
        if message.attachments and not cleaned_content:
            await message.reply("🚨 現在、画像以外の添付ファイルは処理できません。メッセージ本文を入力してください。")
            return
            
        if not cleaned_content:
             await message.reply("メッセージ内容が見つかりませんでした。テキストを入力してください。")
             return
            
        # メッセージ処理のコア関数を実行
        await _process_message_with_ai(message, cleaned_content)

    bot.run(TOKEN)