<?php

namespace App\Scraper;

use App\Model\ComicInfo;
use App\Model\ScrapeResult;
use Symfony\Contracts\HttpClient\Exception\HttpExceptionInterface;
use Symfony\Contracts\HttpClient\Exception\TransportExceptionInterface;
use Symfony\Contracts\HttpClient\HttpClientInterface;

class BedethequeScraper implements ScraperInterface
{
    private const BASE_URL = 'https://www.bedetheque.com';
    private const COVER_LOOKUP_LIMIT = 10;

    public function __construct(
        private readonly HttpClientInterface $httpClient,
    ) {
    }

    public function getName(): string
    {
        return 'Bedetheque';
    }

    public function supports(string $identifier): bool
    {
        return str_contains($identifier, 'bedetheque.com');
    }

    public function getConfigFields(): array
    {
        return [];
    }

    public function search(string $query): array
    {
        try {
            $seriesList = $this->searchSeriesWithFallbacks($query);
            if (empty($seriesList)) {
                return [];
            }

            $first = $seriesList[0];
            $seriesId = (int) $first['id'];
            $seriesName = $first['label'];

            $response = $this->httpClient->request('GET', self::BASE_URL.'/serie/index/s/'.$seriesId);
            $html = $response->getContent();

            return $this->withCovers($this->parseSeriesAlbums($html, $seriesName));
        } catch (TransportExceptionInterface|HttpExceptionInterface $e) {
            return [];
        }
    }

    public function scrape(string $identifier): ComicInfo
    {
        $url = str_starts_with($identifier, 'http') ? $identifier : self::BASE_URL.$identifier;

        if (preg_match('#/serie-\d+#', $url)) {
            return $this->scrapeFirstAlbumFromSeries($url);
        }

        $response = $this->httpClient->request('GET', $url);
        $html = $response->getContent();

        return $this->parseAlbumPage($html);
    }

    private function scrapeFirstAlbumFromSeries(string $url): ComicInfo
    {
        $response = $this->httpClient->request('GET', $url);
        $html = $response->getContent();

        $doc = new \DOMDocument();
        @$doc->loadHTML('<?xml encoding="UTF-8">'.$html);
        $xpath = new \DOMXPath($doc);

        $series = $this->getXpathText($xpath, '//div[contains(@class, "bandeau-info")]//h1/a');

        $list = self::albumList($xpath);
        if (null !== $list) {
            $items = $list->getElementsByTagName('li');
            foreach ($items as $item) {
                $links = $item->getElementsByTagName('a');
                if (0 === $links->length) {
                    continue;
                }

                $href = $links->item(0)->getAttribute('href');
                if (empty($href) || str_contains($href, 'creation')) {
                    continue;
                }

                $albumUrl = str_starts_with($href, 'http') ? $href : self::BASE_URL.$href;

                return $this->scrape($albumUrl);
            }
        }

        return new ComicInfo(series: $series);
    }

    /**
     * @return array<int, array<string, mixed>>
     */
    private function searchSeriesWithFallbacks(string $query): array
    {
        $candidates = [$query];

        $cleaned = self::cleanVolumeSuffix($query);
        if ($cleaned !== $query) {
            $candidates[] = $cleaned;
        }

        $inverted = self::invertArticle($query);
        if ($inverted !== $query) {
            $candidates[] = $inverted;
        }

        $invertedCleaned = self::invertArticle($cleaned);
        if ($invertedCleaned !== $cleaned && $invertedCleaned !== $inverted) {
            $candidates[] = $invertedCleaned;
        }

        foreach ($candidates as $candidate) {
            $seriesList = $this->searchSeries($candidate);
            if (!empty($seriesList)) {
                return $seriesList;
            }
        }

        return [];
    }

    /** First node matching an XPath expression, or null (DOMXPath::query() returns false on an invalid expression). */
    private static function firstNode(\DOMXPath $xpath, string $expression, ?\DOMNode $context = null): ?\DOMNode
    {
        $nodes = null !== $context ? $xpath->query($expression, $context) : $xpath->query($expression);

        return false === $nodes ? null : $nodes->item(0);
    }

    /**
     * The album list has no images, but each album page has its cover. The first results are
     * fetched in parallel (Symfony HttpClient sends the requests before their contents are read);
     * a page that fails keeps its result without a cover.
     *
     * @param ScrapeResult[] $results
     *
     * @return ScrapeResult[]
     */
    private function withCovers(array $results): array
    {
        $pending = [];
        foreach (\array_slice($results, 0, self::COVER_LOOKUP_LIMIT, true) as $i => $result) {
            try {
                $pending[$i] = $this->httpClient->request('GET', $result->pageUrl ?? $result->identifier);
            } catch (TransportExceptionInterface|HttpExceptionInterface) {
                continue;
            }
        }

        foreach ($pending as $i => $response) {
            try {
                $cover = $this->coverFromAlbumPage($response->getContent());
            } catch (TransportExceptionInterface|HttpExceptionInterface) {
                continue;
            }

            $result = $results[$i];
            $results[$i] = new ScrapeResult(
                identifier: $result->identifier,
                title: $result->title,
                subtitle: $result->subtitle,
                year: $result->year,
                publisher: $result->publisher,
                coverUrl: $cover,
                pageUrl: $result->pageUrl,
            );
        }

        return $results;
    }

    private function coverFromAlbumPage(string $html): ?string
    {
        $doc = new \DOMDocument();
        @$doc->loadHTML('<?xml encoding="UTF-8">'.$html);
        $src = self::firstNode(new \DOMXPath($doc), '//img[@itemprop="image"]/@src')?->nodeValue;
        if (null === $src || '' === $src) {
            return null;
        }

        return str_starts_with($src, 'http') ? $src : self::BASE_URL.$src;
    }

    /** The album list of a series page. The site renamed it from liste-albums to bdt-box-albums-liste. */
    private static function albumList(\DOMXPath $xpath): ?\DOMElement
    {
        $list = self::firstNode($xpath, "//ul[contains(@class, 'bdt-box-albums-liste')]");

        return $list instanceof \DOMElement ? $list : null;
    }

    private static function invertArticle(string $query): string
    {
        $trimmed = trim($query);
        if (preg_match('/^((?:Le|La|Les|Un|Une|Des)\s+|L[’\'])(.+)$/iu', $trimmed, $m)) {
            return trim($m[2]).' ('.trim($m[1]).')';
        }

        return $query;
    }

    private static function cleanVolumeSuffix(string $query): string
    {
        $patterns = [
            '/\s*#\d+\s*$/',
            '/\s+Tome\s+\d+\s*$/i',
            '/\s+Volume\s+\d+\s*$/i',
            '/\s+Vol\.?\s*\d+\s*$/i',
            '/\s+-\s+\d+\s*$/',
            '/\s+\d+\s*$/',
        ];

        foreach ($patterns as $pattern) {
            $cleaned = preg_replace($pattern, '', $query);
            if ($cleaned !== $query) {
                return trim($cleaned);
            }
        }

        return $query;
    }

    /**
     * @return array<mixed>
     */
    private function searchSeries(string $query): array
    {
        $response = $this->httpClient->request('GET', self::BASE_URL.'/ajax/series', [
            'query' => ['term' => $query],
        ]);

        return json_decode($response->getContent(), true) ?? [];
    }

    /**
     * @return list<ScrapeResult>
     */
    private function parseSeriesAlbums(string $html, string $seriesName): array
    {
        $results = [];
        $doc = new \DOMDocument();
        @$doc->loadHTML('<?xml encoding="UTF-8">'.$html);
        $xpath = new \DOMXPath($doc);

        $list = self::albumList($xpath);
        if (null === $list) {
            return [];
        }

        $items = $list->getElementsByTagName('li');

        foreach ($items as $item) {
            $links = $item->getElementsByTagName('a');
            if (0 === $links->length) {
                continue;
            }

            $link = $links->item(0);
            $href = $link->getAttribute('href');
            // The album title is in its own span; the link also holds the number and the date.
            $titleNode = self::firstNode($xpath, './/span[contains(@class, "bdt-box-albums-titre")]', $link);
            $title = trim(preg_replace('/\s+/', ' ', $titleNode ? $titleNode->textContent : $link->textContent) ?? '');

            if (empty($href) || empty($title)) {
                continue;
            }
            if (str_contains($href, 'creation')) {
                continue;
            }

            $number = null;
            if (preg_match('/Tome-(\d+)-/', $href, $m)) {
                $number = (int) $m[1];
            }

            $displayTitle = $seriesName;
            if (null !== $number) {
                $displayTitle .= ' #'.$number;
            }
            $displayTitle .= ' - '.$title;

            $pageUrl = str_starts_with($href, 'http') ? $href : self::BASE_URL.$href;

            $results[] = new ScrapeResult(
                identifier: $pageUrl,
                title: $displayTitle,
                subtitle: null !== $number ? (string) $number : null,
                year: null,
                publisher: null,
                coverUrl: null,
                pageUrl: $pageUrl,
            );
        }

        return $results;
    }

    private function parseAlbumPage(string $html): ComicInfo
    {
        $doc = new \DOMDocument();
        @$doc->loadHTML('<?xml encoding="UTF-8">'.$html);
        $xpath = new \DOMXPath($doc);

        $series = $this->getXpathText($xpath, '//h1/a');

        // Microdata: "Série - Titre" in name, "Tome N" in alternativeheadline.
        $fullName = $this->getXpathText($xpath, '//meta[@itemprop="name"]/@content');
        $title = null;
        if (null !== $fullName && $fullName !== $series) {
            $title = null !== $series && str_starts_with($fullName, $series.' - ')
                ? trim(substr($fullName, \strlen($series) + 3))
                : $fullName;
        }

        $number = null;
        $headline = $this->getXpathText($xpath, '//meta[@itemprop="alternativeheadline"]/@content');
        if (null !== $headline && preg_match('/(\d+)/', $headline, $m)) {
            $number = (int) $m[1];
        }

        $writer = null;
        $artist = null;
        $penciler = null;
        $inker = null;
        $colorist = null;
        // Each author is <span><a>Name</a> <small>(Role)</small></span>.
        foreach ($xpath->query('//div[contains(@class, "liste-auteurs")]/span') ?: [] as $authorNode) {
            $nameNode = self::firstNode($xpath, './/a', $authorNode);
            $roleNode = self::firstNode($xpath, './/small', $authorNode);
            if (!$nameNode || !$roleNode) {
                continue;
            }

            $name = trim($nameNode->textContent);
            $role = trim($roleNode->textContent);
            // Placeholders such as "<Quadrichromie>" for colouring are not people.
            if ('' === $name || str_starts_with($name, '<')) {
                continue;
            }

            if (str_contains($role, 'Scénario')) {
                $writer ??= $this->normalizeAuthorName($name);
            } elseif (str_contains($role, 'Dessin')) {
                $artist ??= $this->normalizeAuthorName($name);
                $penciler ??= $artist;
            } elseif (str_contains($role, 'Encre')) {
                $inker ??= $this->normalizeAuthorName($name);
            } elseif (str_contains($role, 'Couleur')) {
                $colorist ??= $this->normalizeAuthorName($name);
            }
        }

        $publisher = $this->getXpathText($xpath, '//span[@itemprop="publisher"]');

        $yearRaw = $this->getXpathText($xpath, '//meta[@itemprop="datePublished"]/@content');
        $year = null !== $yearRaw && preg_match('/^(\d{4})/', $yearRaw, $m) ? (int) $m[1] : null;

        $isbn = $this->getXpathText($xpath, '//input[@id="EAN"]/@value') ?: null;

        $summary = null;
        $descNode = $xpath->query('//span[@itemprop="description"]')->item(0);
        if ($descNode) {
            $summary = trim($descNode->textContent);
            $summary = preg_replace('/\s+/', ' ', $summary);
            $summary = str_replace('Lire la suite', '', $summary);
            $summary = trim($summary);
        }

        return new ComicInfo(
            series: $series,
            title: $title,
            number: $number,
            writer: $writer,
            artist: $artist,
            penciler: $penciler,
            inker: $inker,
            colorist: $colorist,
            publisher: $publisher,
            year: $year,
            isbn: $isbn,
            summary: $summary,
            languageIso: 'fr',
        );
    }

    private function getXpathText(\DOMXPath $xpath, string $expr, ?\DOMNode $context = null): ?string
    {
        $node = null !== $context
            ? $xpath->query($expr, $context)->item(0)
            : $xpath->query($expr)->item(0);

        return $node ? trim($node->textContent) : null;
    }

    private function normalizeAuthorName(string $name): string
    {
        $name = trim($name);
        if (preg_match('/^(.+?),\s*(.+)$/', $name, $m)) {
            return trim($m[2]).' '.trim($m[1]);
        }

        return $name;
    }
}
