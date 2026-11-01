# ProtonDB Discord Bot

A Discord bot for checking **Linux/Proton game compatibility** and looking up useful **Steam game information** without leaving Discord.

Originally created for the [Playden PS5 project](https://github.com/leolobato/playden-ps5) community to make it easier to check Proton compatibility before trying Steam games through Playden. It also includes a few quality-of-life features for Steam game information.

**[Join the Playden Discord](https://discord.gg/wd8UjExpPA)**

> **Note:** ProtonDB reports reflect community experiences running games through Proton. They do **not** guarantee that a game will run on Playden or on a PS5. Hardware, drivers, runtime versions, and other platform differences can affect compatibility.

## Features

- Search for Steam games using slash-command autocomplete.
- Check ProtonDB compatibility tiers and community reports.
- View Steam store details, current regional pricing and Steam user-review ratings.
- Open separate **Requirements** and **Reviews** embeds using buttons.
- Read recent English-language Steam game announcements.\n- See the latest identifiable official game update directly in `/game view:Steam`.\n- Open Steam, ProtonDB, SteamDB and PCGamingWiki through link buttons.
- Look up games by Steam App ID.
- Docker-based deployment with configurable caching and Steam region/language.
- Server-configurable embed colour, visibility, links, report/news counts, moderator role and allowed channels.\n- Persistent settings stored in a Docker volume.\n- Anti-cheat and Steam Deck toggles are reserved for future data integrations; they do not currently fetch compatibility statuses.

## Commands

| Command | Description |
| --- | --- |
| `/game` | Search a game and view its ProtonDB compatibility tier, community reports and Linux-focused troubleshooting. |
| `/settings` | Moderator-only interactive server settings menu. |
| `/game view:Steam` | Show Steam game details, rating, price and links. Use **Requirements** and **Reviews** buttons for additional information. |
| News button | Show recent Steam announcements for a game. |

All game commands accept optional `game`, `appid` and `visibility` (Public or Private). Provide a game name or an App ID. Public is the default unless moderators change it. Game-name commands support autocomplete. Some data may be unavailable if Steam or ProtonDB changes an endpoint, limits requests, or has no information for the selected game.

## Invite or self-host

The bot was created for the Playden community, but you can also host your own instance using Docker.

### Requirements

- Docker Engine and Docker Compose plugin
- A Discord application with a bot token
- Outbound internet access to Discord, Steam and ProtonDB

### Setup

1. Clone the repository:

   ```bash
   git clone https://github.com/isaacm0880/ProtonDB-Discord-Bot.git
   cd ProtonDB-Discord-Bot
   ```

2. Create `.env` in the project directory:

   ```env
   DISCORD_TOKEN=YOUR_DISCORD_BOT_TOKEN
   DISCORD_GUILD_ID=
   CACHE_SECONDS=900
   LOG_LEVEL=INFO
   STEAM_COUNTRY=gb
   STEAM_LANGUAGE=english
   ```

   `DISCORD_GUILD_ID` is optional. Set it to a test server ID for faster slash-command registration during development; leave it empty for global commands. `STEAM_COUNTRY` defaults to `gb` and `STEAM_LANGUAGE` defaults to `english`. You can also set `HTTP_TIMEOUT_SECONDS` (default `12`).

3. In the [Discord Developer Portal](https://discord.com/developers/applications), select your application, enable the bot, and generate an invite link under **Installation** with the following settings:

   **Scopes:**
   - `bot`
   - `applications.commands`

   **Bot Permissions:**
   - View Channels
   - Send Messages
   - Embed Links

   The bot uses slash commands and interactive buttons, so it does **not** require Administrator permissions or privileged intents such as Message Content.

   Once configured, use the generated invite link to add the bot to your Discord server.

4. Start the bot:

   ```bash
   docker compose up -d --build
   ```

5. View logs:

   ```bash
   docker compose logs -f
   ```

### Testing the feature branch\n\nRun this branch with a separate Discord bot token if production is still online; running two instances with the same token can interfere with command syncing.\n\n```bash\ngit fetch origin\ngit switch --track origin/feature/compatibility-settings-v2\ndocker compose up -d --build\n```\n\n### Updating

```bash
git pull
docker compose up -d --build
```

### Troubleshooting

- **Bot offline:** Check the container logs and verify `DISCORD_TOKEN`.
- **Slash commands missing:** Check the application's invite scopes. Global command updates can take time to appear.
- **Missing game data:** Some Steam titles have no public price, requirements, reviews or announcements. ProtonDB individual-report retrieval relies on public data that can change.
- **Unexpected errors:** Review `docker compose logs -f` and open a GitHub issue with non-sensitive logs and steps to reproduce.

## Contributing

Issues and pull requests are welcome. When reporting a bug, include the command, Steam App ID, expected behaviour, actual behaviour and relevant logs. **Remove tokens, credentials and other sensitive information before posting logs.**

## Credits and acknowledgements

- [Playden PS5](https://github.com/leolobato/playden-ps5) — the community this bot was originally built to support.
- [ProtonDB](https://www.protondb.com/) — community Proton compatibility reports.
- [Steam](https://store.steampowered.com/) — game information, reviews, pricing and news.
- [discord.py](https://discordpy.readthedocs.io/) — Discord bot framework.

This is an independent community project and is not officially affiliated with Valve, Steam or ProtonDB.

## License

Released under the [MIT License](LICENSE).

## Unified game command

Use `/game game:Cyberpunk 2077` (ProtonDB by default), `/game game:Cyberpunk 2077 view:Steam`, or `/game appid:1091500 visibility:Private`. Switch between ProtonDB and Steam using the dropdown on the result. The News button opens recent news privately. The old `/steam`, `/protondb`, and `/news` commands are removed.
