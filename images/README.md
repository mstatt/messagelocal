# MessageLocal

A read-only, offline archive for your email. It copies mail from your mail
server over IMAP, or imports a backup file, and gives you a local web UI to
browse, search and open messages and attachments. It cannot send or receive mail.

## Run

```
docker compose up -d --build
```

Open http://localhost:9950 and click **Add mail**.

- **Connect server**: enter your email address and password. The IMAP server
  defaults to `mail.<your domain>` on port 993, which is the usual cPanel setup.
  The first sync pulls everything; **Sync now** later fetches only new mail.
- **Upload backup**: a cPanel backup (`.tar.gz`), Maildir folders or `.eml`
  files in a `.zip`/`.tar.gz`, or an `.mbox` file.

Internet access is needed only while syncing. Reading works fully offline.

## Two views

Switch with the **List / Map** control; your choice is remembered.

- **List**: the classic three-pane reader.
- **Map**: your mailbox as a constellation. The orb is the mailbox (click it to
  switch); the lenses stacked under it are People, Folders, Long threads,
  Attachments and Years; the branches are that lens's contents.
  - **Scroll** over the map (or use the arrows at the bottom right) to spin
    through everything in a lens.
  - **Search bar**: filters the current lens as you type; Enter searches all
    mail in that mailbox.
  - **Side panel**: activity, key numbers and messages for the selected branch.
    Click *With attachments* or *Conversations* to filter the list.
  - **Click a message** for a preview card beside the list; double-click or
    press Enter to open it in full.
  - **Panel handle**: drag it to resize the panel, click it to hide or show.
  - Keys: `1`-`5` lens, arrows move, `PgUp`/`PgDn` spin, `Enter` preview/open,
    `/` search, `Esc` back.

Both views adapt to tablets and phones: on small screens the mailbox list
becomes a slide-out menu and messages open full screen.

## Timeline

A bar along the bottom of both views shows all your mail on one time axis,
across every mailbox, folder and sender (hidden ones excluded). Taller bars are
busier months.

- **Hover** to see the message at that point in time.
- **Click** to pin the preview; **scroll** to step one message at a time.
- **Click the preview** (or double-click the bar) to open the message in a
  large window. The arrows on either side, or the left/right keys, walk
  through the timeline; clicking the bar jumps to another date.

## Hiding things and the default mailbox

In the List view, hover a mailbox or folder:

- **Eye icon** hides it. Hidden items disappear from both views, from "All
  mail", from search and from the Map's counts. They still sync and stay on
  disk. "Show hidden" at the bottom of the sidebar brings them back.
- **Star icon** on a mailbox makes it the one that opens when you start the app.

## What it does and doesn't do

- Opens mailboxes read-only. Nothing is changed, marked read or deleted on the server.
- Never deletes from the archive. Mail removed from the server later stays here.
- Skips duplicates, so re-syncing or importing a backup of an already-synced
  mailbox adds only what is missing.
- Shows image and video attachments inline beneath the message text; other
  attachments stay as download chips.
- Shows HTML mail in a sandbox with scripts disabled and remote images blocked
  (there is a per-message button to load them).

## Your data

Everything is in `./data`:

| Path | Contents |
|---|---|
| `data/mail/<account>/` | Every message as a standard `.eml` file, attachments included |
| `data/messagelocal.db` | Index and search database (can be rebuilt from the `.eml` files) |
| `data/secret.key` | Key that encrypts your saved mail passwords |

Back up the `data` folder to keep a second copy. The `.eml` files open in any
mail program, so the archive does not depend on this app.

Saved passwords are encrypted, but by default the key sits beside them in
`data/`. To keep the key out of the folder, set `MESSAGELOCAL_KEY` in
`docker-compose.yml` before adding accounts.

## Troubleshooting

- **Certificate doesn't match**: shared hosts often present the hosting
  company's certificate. Use the server name your host lists under cPanel →
  Email Accounts → Connect Devices, or untick "Verify the server's certificate".
- **Authentication failed**: the username is normally the full email address.
- **Changed your mail password**: add the account again with the same email
  address; it updates the saved password and keeps the archive.
- Logs: `docker logs messagelocal`
