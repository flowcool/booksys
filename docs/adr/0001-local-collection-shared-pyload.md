# Local collection and one shared downloader

Booksys uses Byparr for browser-session access and pyLoad for download resolution and transfer. Reuse one downloader instead of creating an additional queue and configuration store. Preserve unowned packages and downloaded originals.

## Downloader support

Booksys submits collected link sets to pyLoad. The optional AllDebrid integration uses the provider's supported-hoster list; availability and quotas can change. Captcha support depends on pyLoad plugins and the operator's configuration. No provider account, activation, balance or successful challenge is implied.

## Public references

- http://byparr:8191/v1
- http://pyload:8000/api
- https://www7.bookys-ebooks.com/bandes-dessinees/new
