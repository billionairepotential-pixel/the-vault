# The Vault

A Yu-Gi-Oh! deck collection: 20 decks in six chapters.
Live site: https://billionairepotential-pixel.github.io/the-vault/

## Updating a deck

1. Export the deck from your simulator as a `.ydk` file.
2. On GitHub, open the `ydk` folder, choose **Add file → Upload files**, and upload it with the deck's existing file name (for example `ignister.ydk`), replacing the old one.
3. Commit. That's it.

About two minutes later the site has been rebuilt. The deck's decklist, card counts, Extra Deck breakdown and hand odds are updated, and its "List updated" date is set to that day. The build also adds card text for any card that is new to the deck, and fresh offline copies appear on the [offline release](https://github.com/billionairepotential-pixel/the-vault/releases/tag/offline).

Open the **Actions** tab and click the latest run to see its summary. It lists what changed and anything that needs a human look, for example:

- card text imported from YGOPRODeck that should be checked against Konami's database
- new cards that aren't sorted into the hand-odds roles yet
- decks whose ratings in the collection report are older than their list
- banned or over-limit cards

If a run fails (a red ✗), the live site is left as it was. The summary says what to fix.

## Changing text on the site

| To change | Edit |
|---|---|
| Chapter titles, definitions and intros; deck names, labels, taglines, best opening hands | `content/decks.json` |
| Collection report: ratings, verdicts, hand-odds roles, breaker counts | `content/analysis.json` |
| Deck notes (engine, combo lines, strengths, weaknesses) | `content/notes.json` |
| Card text in the decklist viewer | `data/cards.json` |
| Layout, styles and behaviour | `build/template.html` |

Don't edit `index.html` or `content/decklists.json` directly. The build rewrites both from the files above.

Photos go in `art/` (deck photos) and `cards/` (card photos) at full quality. The build makes smaller copies in `web/` for the live site, so it loads quickly; don't edit `web/` by hand.

## Offline copies

The [offline release](https://github.com/billionairepotential-pixel/the-vault/releases/tag/offline) always has the latest:

- **The-Vault-offline-standalone.html**: one file that opens anywhere, with smaller photos built in.
- **The-Vault-offline-package.zip**: unzip it and open `index.html`. This copy keeps the full-quality photos.

## Building on your own computer (optional)

```
pip install Pillow
python build/build.py --dist dist
```
