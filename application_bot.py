import asyncio
import json
import os
import re

import discord
import gspread
from discord import app_commands
from discord.ext import commands, tasks

TOKEN = os.getenv("DISCORD_TOKEN")  # set this as an environment variable

# ------------------------------------------------------------------ Discord IDs
RESULTS_CHANNEL_ID = 1507974351381725226
STAFF_ROLE_ID = 1538163810958057522
REVIEW_CHANNEL_ID = 0  # CHANGE THIS: channel where new applications get posted for staff

# --------------------------------------------------------------- Google Sheets
CREDENTIALS_FILE = "service_account.json"  # Google service account key file
STATE_FILE = "processed_rows.json"  # remembers which rows were already posted
SCAN_MINUTES = 2
POST_EXISTING_ON_FIRST_RUN = False  # True = post every row already in the sheets

# One entry per department. Share each sheet with the service account email.
SHEETS = [
    {"name": "Flight Deck", "id": "1RGzorIUqeoT8gJY1GIBz-AhX5o6sgXXAmeVL-TglVrw"},
    {"name": "Public Relations", "id": "1wiDvcWbxiN2hr_7-oJBExdkclRuxlPEYb1uRd6FiVZA"},
    {"name": "Cabin Crew", "id": "1TJe5Zg0ja549TjxK1qENVtXgOLsPU2sRSxoVdTwAPL0"},
    {"name": "Human Resources", "id": "10iZQEyVevDYtn9t99l1BOmkBpRa6JG58_4_R94pFBog"},
    {"name": "Ground Crew", "id": "13E1JnyVKNBCD_37QJqaM38TFZy7UD8jCrvHaJqleVr0"},
]

# Shown at the top of each application (matched by words in the column header).
SUMMARY_FIELDS = [
    ("Discord User", "discord user"),
    ("Discord ID", "discord id"),
    ("Roblox Username", "roblox username"),
    ("Age Group", "age-group"),
    ("13 or Above", "13-years"),
    ("Timezone", "timezone"),
    ("Device", "device will you attend"),
    ("Flight Minutes", "flight minutes"),
]
# Columns never shown in the channel.
HIDDEN_HEADERS = ("timestamp", "email address")

GREEN = discord.Color.from_rgb(46, 204, 113)
RED = discord.Color.from_rgb(231, 76, 60)
BLUE = discord.Color.from_rgb(52, 152, 219)

intents = discord.Intents.default()
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)


# ------------------------------------------------------------------ State
def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {}


def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


# ------------------------------------------------------------------ Sheets
def fetch_rows(sheet_id: str):
    gc = gspread.service_account(filename=CREDENTIALS_FILE)
    return gc.open_by_key(sheet_id).sheet1.get_all_values()


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def build_embeds(department: str, headers, row, row_number):
    """Returns a summary embed plus one or more answer embeds."""
    lowered = [h.lower() for h in headers]
    used = set()

    summary = discord.Embed(
        title=f"New Application - {department}",
        description="A new application has been submitted.",
        color=BLUE,
    )

    for label, keyword in SUMMARY_FIELDS:
        for i, h in enumerate(lowered):
            if keyword in h and i not in used and i < len(row):
                value = clean(row[i]) or "N/A"
                if label == "Discord ID":
                    digits = re.sub(r"\D", "", value)
                    if digits:
                        value = f"<@{digits}> ({digits})"
                summary.add_field(name=label, value=value[:1024], inline=True)
                used.add(i)
                break

    timestamp = row[0] if row else ""
    summary.set_footer(text=f"Sheet row {row_number} | Submitted {timestamp}")

    # Everything else becomes the answers section, split to fit Discord limits.
    answer_embeds = []
    current = discord.Embed(title="Answers", color=BLUE)
    size = 0
    for i, header in enumerate(headers):
        if i in used or i >= len(row):
            continue
        if any(h in lowered[i] for h in HIDDEN_HEADERS):
            continue
        name = clean(header)[:256] or "Question"
        value = clean(row[i])[:1024] or "N/A"
        if len(current.fields) >= 25 or size + len(name) + len(value) > 5000:
            answer_embeds.append(current)
            current = discord.Embed(title="Answers (continued)", color=BLUE)
            size = 0
        current.add_field(name=name, value=value, inline=False)
        size += len(name) + len(value)
    if current.fields:
        answer_embeds.append(current)

    return [summary] + answer_embeds


async def process_sheet(sheet: dict, state: dict, channel: discord.abc.Messageable):
    try:
        rows = await asyncio.to_thread(fetch_rows, sheet["id"])
    except Exception as e:
        print(f"[{sheet['name']}] sheet read failed: {e}")
        return

    if not rows:
        return

    headers, data = rows[0], rows[1:]
    processed = state.get(sheet["id"])

    if processed is None:
        processed = 0 if POST_EXISTING_ON_FIRST_RUN else len(data)
        state[sheet["id"]] = processed
        save_state(state)

    if len(data) < processed:  # rows were deleted from the sheet
        processed = len(data)
        state[sheet["id"]] = processed
        save_state(state)

    for offset in range(processed, len(data)):
        row = data[offset]
        row_number = offset + 2  # +1 for header, +1 for 1-indexing

        if any(cell.strip() for cell in row):
            try:
                for embed in build_embeds(sheet["name"], headers, row, row_number):
                    await channel.send(embed=embed)
            except discord.HTTPException as e:
                print(f"[{sheet['name']}] could not post row {row_number}: {e}")
                return  # state not advanced, retried next scan

        state[sheet["id"]] = offset + 1
        save_state(state)


@tasks.loop(minutes=SCAN_MINUTES)
async def scan_sheets():
    channel = bot.get_channel(REVIEW_CHANNEL_ID)
    if channel is None:
        print("Review channel not found. Check REVIEW_CHANNEL_ID.")
        return

    state = load_state()
    for sheet in SHEETS:
        await process_sheet(sheet, state, channel)


@scan_sheets.before_loop
async def before_scan():
    await bot.wait_until_ready()


# ------------------------------------------------------------------ Bot events
@bot.event
async def setup_hook():
    await bot.tree.sync()
    scan_sheets.start()


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} ({bot.user.id})")


# ------------------------------------------------------------------ /application
@bot.tree.command(name="application", description="Accept or deny an application")
@app_commands.describe(
    result="Accept or deny the application",
    user="The applicant",
    application_type="The department they applied for",
    notes="Additional notes for the applicant",
)
@app_commands.choices(
    result=[
        app_commands.Choice(name="Accept", value="accept"),
        app_commands.Choice(name="Deny", value="deny"),
    ],
    application_type=[
        app_commands.Choice(name=s["name"], value=s["name"]) for s in SHEETS
    ],
)
@app_commands.checks.has_role(STAFF_ROLE_ID)
async def application(
    interaction: discord.Interaction,
    result: app_commands.Choice[str],
    user: discord.Member,
    application_type: app_commands.Choice[str],
    notes: str = "None",
):
    channel = interaction.guild.get_channel(RESULTS_CHANNEL_ID)
    if channel is None:
        try:
            channel = await interaction.guild.fetch_channel(RESULTS_CHANNEL_ID)
        except discord.NotFound:
            await interaction.response.send_message(
                "I could not find the application results channel.", ephemeral=True
            )
            return

    accepted = result.value == "accept"

    if accepted:
        description = (
            "Thank you for taking the time to apply for a career here at "
            "Emirates. Welcome to the team! Please keep track of your "
            "notifications until further notice."
        )
        color = GREEN
    else:
        description = (
            "Thank you for taking the time to apply for a career here at "
            "Emirates. Unfortunately, your application has not been successful "
            "this time. You are welcome to apply again in the future."
        )
        color = RED

    embed = discord.Embed(
        title="Application Result",
        description=description,
        color=color,
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(name="User", value=user.mention, inline=False)
    embed.add_field(name="Application Type", value=application_type.value, inline=False)
    embed.add_field(
        name="Result", value="Accepted" if accepted else "Denied", inline=False
    )
    embed.add_field(name="Additional notes", value=notes, inline=False)
    embed.set_footer(text=f"Reviewed by {interaction.user.display_name}")

    await channel.send(
        content=user.mention,
        embed=embed,
        allowed_mentions=discord.AllowedMentions(users=True),
    )

    await interaction.response.send_message(
        f"Application result sent to {channel.mention}.", ephemeral=True
    )


@application.error
async def application_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
):
    if isinstance(error, app_commands.MissingRole):
        await interaction.response.send_message(
            "You do not have permission to use this command.", ephemeral=True
        )
    else:
        raise error


bot.run(TOKEN)
