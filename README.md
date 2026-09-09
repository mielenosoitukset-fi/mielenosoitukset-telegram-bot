# Mielenosoitukset.fi – Telegram Bot

Telegram-botti joka ilmoittaa uusista mielenosoituksista
[mielenosoitukset.fi](https://mielenosoitukset.fi) -palvelusta.

Käyttäjä voi tilata ilmoituksia **kaupungin**, **järjestön** tai
**toistuvan mielenosoitusketjun** mukaan — suoraan Telegramista,
helpoilla painikkeilla (simppeli!).

## Ominaisuudet

- 🏙️ Tilaa kaikki mielenosoitukset valitsemassasi kaupungissa
- 🏢 Tilaa tietyn järjestön järjestämät mielenosoitukset (haku nimellä tai listasta)
- 🔁 Tilaa toistuvia mielenosoitusketjuja (esim. viikoittaiset mielenosoitukset)
- 📣 Automaattinen ilmoitus aina kun uusi mielenosoitus lisätään
- 🔐 API tokenin konfigurointi suoraan botista (lyhyt → 90pv pitkä token)

## Ylösajo

### 1. Valmistele `.env`

```bash
cp .env.example .env
```

Aseta ainakin `TELEGRAM_BOT_TOKEN`.

### 2. Asenna riippuvuudet

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

### 3. Käynnistä

```bash
mielenosoitukset-bot
# tai
python -m bot.main
```

### 4. Konfiguroi API-token

Aseta `ADMIN_CHAT_IDS` (pilkuilla erotellut Telegram-chat ID:t) `.env`-tiedostoon —
vain nämä voivat konfiguroida tokenin. Sen jälkeen lähetä botille:

```
/config <lyhytaikainen-token>
```

Botti vaihtaa tokenin automaattisesti pitkäaikaiseksi (90 päivää)
palvelun API:n kautta (`POST /token/long_lived`) ja tallentaa sen levylle.
Voidaan asettaa myös suoraan `API_TOKEN`-ympäristömuuttujaan (silloin
vaihtoa ei tarvita). Päivitä katalogi `/paivita` ja tarkista tila `/status`.

## Docker

```bash
docker compose up -d --build
```

## Arkkitehtuuri

- **Pollaus**: botti kysyy mielenosoitukset.fi-API:a säännöllisesti (`POLL_MINUTES`)
- **Tilaukset**: tallennetaan SQLite-tietokantaan (`data/bot.db`)
- **Katalogi**: kaupungit, järjestöt ja ketjut haetaan API:sta ja välimuistitetaan

## Lisenssi

MIT
