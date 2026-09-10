# Changelog

## UNRELEASED

- Fix: commands now also work in channels (previous CommandHandler filter ignored `channel_post` updates).
- Fix: `/ryhma` now responds in channels — only channel admins can post, so the admin check is skipped for channel posts.
- Fix: `entity_links` migration now also renames the old `group_chat_id` column, fixing `/liita` and `/hallinta` on databases created before the group/channel merge.
- Initial version of the mielenosoitukset.fi Telegram bot.
- Subscribe to demonstrations by city, organization, or recurring chain.
- Auto-notifications for new demonstrations via polling.
- API token configuration flow (short → long-lived) from within Telegram.
- Docker + docker-compose deployment.
