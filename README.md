# Group Tournament Manager Bot

A Telegram tournament bot designed around group chats. Each Telegram group can have one active tournament at a time. The group administrator who creates the tournament becomes its Tournament Manager and can appoint additional Tournament Admins.

## Features

- Tournament creation inside a Telegram group by a Telegram group admin (or configured bot-wide admin).
- One active tournament per group, enforced by a MongoDB partial unique index.
- Tournament Manager can appoint/remove Tournament Admins by replying to a member's message.
- Tournament name, description, format, and participant capacity.
- Group-only participant registration. `/register` uses the user's Telegram first name automatically; participants are displayed as clickable Telegram mentions throughout participant lists, fixtures, results, and standings.
- Knockout and round-robin fixtures.
- Match result recording and automatic knockout advancement.
- Group announcements that the bot posts and pins, with automatic unpin after a configurable duration; a new announcement unpins the previous tracked tournament announcement. Pending unpins are stored in MongoDB and are rechecked after restarts.
- Tournament history and participant/manager commands.

## Requirements

- Python 3.10+
- MongoDB (Atlas or self-hosted)
- A Telegram bot created with [@BotFather](https://t.me/BotFather)

## Installation

1. Extract the project archive.
2. Create and activate a virtual environment.
3. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

4. Copy `.env.example` to `.env` and fill in `BOT_TOKEN` and `MONGODB_URI`.
5. Start the bot:

   ```bash
   python bot.py
   ```

## Telegram group setup

1. Add the bot to the desired group.
2. Promote it to group admin and grant at least **Pin Messages** permission. It needs normal send-message permission too.
3. A Telegram group admin creates the tournament using `/newtournament`.
4. The creator becomes the Tournament Manager. The manager can appoint Tournament Admins by replying to a member's message with `/addtouradmin` or by using `/addtouradmin USER_ID`.
5. Participants use `/register` inside that group. They do not type their names; the bot automatically uses their Telegram first name.

The bot must have permission to pin messages for announcement pinning to work. Auto-unpin requires the included JobQueue dependency. If `ANNOUNCEMENT_PIN_HOURS=0`, auto-unpin is disabled and the manager can use `/unpinannouncement`.

## Commands

### Participants (group unless specified)

| Command | Purpose |
|---|---|
| `/start` | Start the bot in private chat |
| `/help` | Show commands |
| `/tournament` | Show the group's active/latest tournament |
| `/tournaments` | Show tournament history for the group |
| `/register` | Register using Telegram first name (group only) |
| `/unregister` | Withdraw before fixtures are generated (group only) |
| `/rules` | Show current group's rules |
| `/fixtures` | Show fixtures/results |
| `/participants` | Show registered members as clickable mentions |
| `/standings` | Round-robin standings |
| `/myregistrations` | Show your registrations |
| `/myid` | Show your Telegram user ID |

Private-chat viewing commands may include a tournament ID, e.g. `/fixtures A1B2C3`. Registration and unregistration remain group-only.

### Group admins / Tournament Manager / Tournament Admins

| Command | Permission | Purpose |
|---|---|---|
| `/newtournament Name | format | capacity | description` | Telegram group admin | Create the group's tournament |
| `/addtouradmin` (reply or `USER_ID`) | Tournament Manager | Appoint Tournament Admin |
| `/removetouradmin` (reply or `USER_ID`) | Tournament Manager | Remove Tournament Admin |
| `/setdescription Description...` | Manager/Tournament Admin | Update description |
| `/setrules Rules...` | Manager/Tournament Admin | Update rules |
| `/open` | Manager/Tournament Admin | Reopen registration before fixtures |
| `/close` | Manager/Tournament Admin | Close registration |
| `/generatefixtures` | Manager/Tournament Admin | Generate fixtures and close registration |
| `/setresult MATCH_ID WINNER_USER_ID` | Manager/Tournament Admin | Record match winner |
| `/announce Message...` | Manager/Tournament Admin | Post and pin a group announcement |
| `/unpinannouncement` | Manager/Tournament Admin | Unpin latest tracked announcement |
| `/complete` | Manager/Tournament Admin | Complete tournament and release group for next tournament |
| `/adminparticipants` | Manager/Tournament Admin | Show participant IDs |
| `/admintournaments` | Group members | Show tournament history in group |

### Tournament creation examples

```text
/newtournament College Gaming Cup | knockout | 16 | A knockout championship for college students.
/newtournament College Football League | round_robin | 12 | Everyone plays every other team.
```

Description is optional. Valid formats are `knockout` and `round_robin`. Capacity must be between 2 and 512.

### Recording a result

Use `/adminparticipants` to find each player's Telegram user ID. Then submit:

```text
/setresult R1-M1 123456789
```

The match ID is visible in `/fixtures`. In knockout tournaments, the winner advances automatically. The final knockout result completes the tournament automatically.

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `BOT_TOKEN` | Yes | Telegram bot token |
| `MONGODB_URI` | Yes | MongoDB connection URI |
| `DATABASE_NAME` | No | Defaults to `tournament_manager` |
| `ADMIN_IDS` | No | Comma-separated bot-wide super-admin Telegram IDs |
| `ANNOUNCEMENT_PIN_HOURS` | No | Defaults to 24; 0 disables auto-unpin |

## Important behavior

- Only Telegram group admins (or optional bot-wide admins) can create a tournament in a group.
- The creator becomes the tournament manager. Tournament Admins are separate from Telegram group admins and only receive tournament-management permissions.
- Only one tournament with `is_active=True` can exist in a group. `/complete` releases the group so a new tournament can be created.
- The bot uses `tg://user?id=...` HTML mentions so a participant's name is clickable. Telegram clients may display names according to the user's current profile.
- The bot does not schedule match start times or verify match outcomes automatically. Tournament staff must enter results.
- To receive group commands, privacy mode can usually remain enabled because commands are explicit. If the bot does not react to commands, confirm it is an admin and check BotFather's group/privacy settings.

## Existing database

The new code adds `group_id`, `manager_id`, `tournament_admins`, `is_active`, and announcement metadata to tournament documents. Existing tournaments created by the previous direct/private workflow have no group binding and cannot be managed as group tournaments through this version. They remain in the database for historical lookups. Back up MongoDB before deploying a new bot version.
