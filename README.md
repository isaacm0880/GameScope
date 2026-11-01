# GameScope

A Discord bot for **Steam game information, ProtonDB compatibility, reviews, requirements, and news**, with interactive views and configurable server settings.

Originally built for the [Playden PS5](https://github.com/leolobato/playden-ps5) community. [Join the Playden Discord](https://discord.gg/wd8UjExpPA).

> **Compatibility disclaimer:** ProtonDB reports describe community experiences with Proton on Linux. They do **not** guarantee that a game will run on Playden, PS5, or every Linux device.

## Features

- Search Steam games by name (with autocomplete) or numeric Steam App ID.
- Switch between **ProtonDB** and **Steam** views using the dropdown on a game result.
- ProtonDB compatibility tier and recent community reports.
- Steam store information, regional pricing, release information and review ratings.
- **Requirements** and **Reviews** buttons for detailed Steam information.
- **News** button in the Steam view for recent announcements, plus the latest identifiable game update in the Steam embed.
- External links to Steam, ProtonDB, SteamDB and PCGamingWiki (the latter two can be disabled per server).
- Public or private results, configurable embed colour and content counts.
- Custom Discord bot presence (status, activity type and text), saved across restarts. The default is **Watching /game**.
- Administrator-only settings by default; administrators can grant additional roles access.
- SQLite persistence using a Docker volume.

## Commands

### `/game`

Look up a game by name or Steam App ID.

| Option | Required | Description |
| --- | --- | --- |
| `game` | Yes | Steam game name or numeric App ID |
| `view` | No | `ProtonDB` (default) or `Steam` |
| `visibility` | No | `Public` or `Private`; defaults to the server setting |

Examples:

```text
/game game:Cyberpunk 2077
/game game:Cyberpunk 2077 view:Steam
/game game:1091500
/game game:Portal 2 visibility:Private
```

Use the dropdown on the result to switch views without issuing another command. The Steam view provides **Requirements**, **Reviews**, and **News** buttons. News opens privately.

### `/settings`

Configure the bot through an interactive Discord menu.

| Section | Options |
| --- | --- |
| Appearance | Default result visibility and embed colour |
| Game information | ProtonDB report count, Steam news count, SteamDB and PCGamingWiki buttons |
| Access | Additional settings roles and allowed channels |
| Bot presence | Online / Idle / Do Not Disturb / Invisible; Playing / Watching / Listening / Competing; custom activity text; enable or disable activity |

**Permissions:** Users with Discord's **Administrator** permission can access settings by default. Administrators can add up to ten additional roles to grant settings access; clearing the role selection restores administrator-only access. Only administrators can grant roles settings access.

**Presence is global:** Administrators and authorised settings roles in any server using the bot can change its presence for all servers.

## Self-hosting with Docker

### Requirements

- Docker Engine and the Docker Compose plugin
- A Discord application and bot token
- Outbound access to Discord, Steam and ProtonDB

### 1. Clone

```bash
git clone https://github.com/isaacm0880/GameScope.git
cd GameScope
```

### 2. Configure environment

Create a `.env` file in the repository directory:

```dotenv
DISCORD_TOKEN=YOUR_DISCORD_BOT_TOKEN
DISCORD_GUILD_ID=
STEAM_COUNTRY=gb
STEAM_LANGUAGE=english
CACHE_SECONDS=900
LOG_LEVEL=INFO
```

`DISCORD_GUILD_ID` is optional: specify a test server ID for faster development command sync, or leave blank for global commands. `HTTP_TIMEOUT_SECONDS` is also supported (default: `12`).

### 3. Invite the bot

In the [Discord Developer Portal](https://discord.com/developers/applications), create or open your application and invite it with the `bot` and `applications.commands` scopes.

Required bot permissions: **View Channels**, **Send Messages**, and **Embed Links**.

### 4. Build and start (first installation)

```bash
docker compose up -d --build
```

### Normal startup

```bash
docker compose up -d
```

To view logs:

```bash
docker compose logs -f
```

### Updating the bot

```bash
git pull --ff-only
docker compose up -d --build
```

## Troubleshooting

- **Bot offline or restarting:** Run `docker compose ps` and `docker compose logs --tail=100` to inspect startup errors.
- **Slash commands missing:** Confirm the invite scopes and guild ID. Global command changes can take time to appear.
- **Settings unavailable:** Only server administrators and roles granted settings access can open `/settings`.
- **Presence changes fail:** Check container logs. Presence updates require an active Discord gateway connection.
- **Missing game information or updates:** Steam and ProtonDB may have incomplete data or rate limits. The update detector is heuristic and may not identify every patch.

## Contributing

Issues and pull requests are welcome. Include reproduction steps, the relevant Steam App ID and sanitized logs. **Never publish your bot token or other credentials.**

## Credits

- [Steam](https://store.steampowered.com/)
- [ProtonDB](https://www.protondb.com/)
- [SteamDB](https://steamdb.info/)
- [PCGamingWiki](https://www.pcgamingwiki.com/)
- [discord.py](https://discordpy.readthedocs.io/)

GameScope is an independent project and is not officially affiliated with Valve, Steam ProtonDB or PCGamingWiki.

## License

[MIT License](LICENSE).

---

> **AI disclosure:** GameScope was built with the assistance of AI.
