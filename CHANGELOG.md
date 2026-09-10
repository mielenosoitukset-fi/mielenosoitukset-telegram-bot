# Changelog

## UNRELEASED

- Fix: org/chain picker buttons no longer break for long names — callback data is capped to Telegram's 64-byte limit and the full name is resolved from the catalog on toggle.
- Fix: stale/too-old callback queries and `Button_data_invalid` edits no longer spam logs.

- Feature: city picker is now split into an alphabet overview with per-letter pages (20 per page) — no more giant scrollable list.
- Feature: city picker supports search: tap "Hae kaupunkia" and type a name (min 2 characters).
- Feature: organization list now loads from the new public `/api/v1/organizations` listing endpoint when available, falling back to scraper results otherwise.
- Fix: silent "message is not modified" edit errors no longer spam logs.
- Fix: commands now also work in channels (previous CommandHandler filter ignored `channel_post` updates).
- Fix: `/ryhma` now responds in channels — only channel admins can post, so the admin check is skipped for channel posts.
- Fix: `entity_links` migration now also renames the old `group_chat_id` column, fixing `/liita` and `/hallinta` on databases created before the group/channel merge.
- Initial version of the mielenosoitukset.fi Telegram bot.
- Subscribe to demonstrations by city, organization, or recurring chain.
- Auto-notifications for new demonstrations via polling.
- API token configuration flow (short → long-lived) from within Telegram.
- Docker + docker-compose deployment.
