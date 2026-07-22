<?php

declare(strict_types=1);

/**
 * Carma trade-in lookup script.
 *
 * Usage:
 *   php carma_lookup.php <REGO> <STATE> <RECAPTCHA_TOKEN>
 *
 * Output:
 *   JSON to stdout only (no file writes for result).
 */

if ($argc < 4) {
    fwrite(STDERR, "Usage: php carma_lookup.php <REGO> <STATE> <RECAPTCHA_TOKEN>\n");
    exit(1);
}

$rego = strtoupper(trim((string)$argv[1]));
$state = strtoupper(trim((string)$argv[2]));
$recaptchaToken = trim((string)$argv[3]);

if ($rego === '' || $state === '' || $recaptchaToken === '') {
    echo json_encode([
        'ok' => false,
        'error' => 'rego, state, and recaptcha token are required',
    ], JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES) . PHP_EOL;
    exit(2);
}

$baseUrl = 'https://carma.com.au';
$formPath = '/forms/-/trade-in-enquiry';
$formUrl = $baseUrl . $formPath . '?' . http_build_query([
    'rego' => $rego,
    'state' => $state,
    'searchType' => 'byRego',
]);
$carFoundUrl = $baseUrl . '/forms/-/trade-in-enquiry/car-found';

$cookieFile = sys_get_temp_dir() . DIRECTORY_SEPARATOR . 'carma_cookie_' . uniqid('', true) . '.txt';

try {
    [$status1] = httpRequest(
        'GET',
        $formUrl,
        [],
        [
            'Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36',
        ],
        $cookieFile
    );

    if ($status1 < 200 || $status1 >= 400) {
        throw new RuntimeException('Initial GET failed with HTTP ' . $status1);
    }

    [$status2] = httpRequest(
        'POST',
        $formUrl,
        [
            '_1_rego' => $rego,
            '_1_state' => $state,
            '_1_searchType' => 'byRego',
            '_1_recaptchaToken' => $recaptchaToken,
        ],
        [
            'Accept: text/x-component, */*;q=0.1',
            'Origin: ' . $baseUrl,
            'Referer: ' . $formUrl,
            'User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36',
        ],
        $cookieFile
    );

    if ($status2 < 200 || $status2 >= 400) {
        throw new RuntimeException('Form POST failed with HTTP ' . $status2);
    }

    [$status3, , $body3] = httpRequest(
        'GET',
        $carFoundUrl,
        [],
        [
            'Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Referer: ' . $formUrl,
            'User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36',
        ],
        $cookieFile
    );

    if ($status3 < 200 || $status3 >= 400) {
        throw new RuntimeException('car-found GET failed with HTTP ' . $status3);
    }

    $parsed = parseCarFoundHtml($body3);
    $car = normalizeCarDetails($parsed, $rego, $state);

    echo json_encode([
        'ok' => true,
        'http' => [
            'initial_get' => $status1,
            'submit_post' => $status2,
            'car_found_get' => $status3,
        ],
        'car' => $car,
    ], JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES) . PHP_EOL;
} catch (Throwable $e) {
    echo json_encode([
        'ok' => false,
        'error' => $e->getMessage(),
    ], JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES) . PHP_EOL;
    exit(2);
} finally {
    if (is_file($cookieFile)) {
        @unlink($cookieFile);
    }
}

/**
 * @return array{0:int,1:array<string,string>,2:string}
 */
function httpRequest(
    string $method,
    string $url,
    array $postFields,
    array $headers,
    string $cookieFile
): array {
    if (function_exists('curl_init')) {
        return httpRequestWithCurlExtension($method, $url, $postFields, $headers, $cookieFile);
    }

    return httpRequestWithCurlCli($method, $url, $postFields, $headers, $cookieFile);
}

/**
 * @return array{0:int,1:array<string,string>,2:string}
 */
function httpRequestWithCurlExtension(
    string $method,
    string $url,
    array $postFields,
    array $headers,
    string $cookieFile
): array {
    $ch = curl_init();
    if ($ch === false) {
        throw new RuntimeException('Unable to initialize cURL extension.');
    }

    curl_setopt_array($ch, [
        CURLOPT_URL => $url,
        CURLOPT_CUSTOMREQUEST => $method,
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_FOLLOWLOCATION => false,
        CURLOPT_HEADER => true,
        CURLOPT_HTTPHEADER => $headers,
        CURLOPT_COOKIEJAR => $cookieFile,
        CURLOPT_COOKIEFILE => $cookieFile,
        CURLOPT_TIMEOUT => 60,
        CURLOPT_CONNECTTIMEOUT => 20,
        CURLOPT_ENCODING => '',
    ]);

    if ($method === 'POST') {
        curl_setopt($ch, CURLOPT_POSTFIELDS, $postFields);
    }

    $raw = curl_exec($ch);
    if ($raw === false) {
        $err = curl_error($ch);
        curl_close($ch);
        throw new RuntimeException('cURL extension request failed: ' . $err);
    }

    $status = (int)curl_getinfo($ch, CURLINFO_HTTP_CODE);
    $headerSize = (int)curl_getinfo($ch, CURLINFO_HEADER_SIZE);
    curl_close($ch);

    $rawHeaders = substr($raw, 0, $headerSize);
    $body = substr($raw, $headerSize);

    $parsedHeaders = parseHeaderBlock($rawHeaders);

    return [$status, $parsedHeaders, (string)$body];
}

/**
 * @return array{0:int,1:array<string,string>,2:string}
 */
function httpRequestWithCurlCli(
    string $method,
    string $url,
    array $postFields,
    array $headers,
    string $cookieFile
): array {
    $headerFile = tempnam(sys_get_temp_dir(), 'carma_hdr_');
    $bodyFile = tempnam(sys_get_temp_dir(), 'carma_body_');
    $configFile = tempnam(sys_get_temp_dir(), 'carma_cfg_');

    if ($headerFile === false || $bodyFile === false || $configFile === false) {
        throw new RuntimeException('Unable to create temp files for curl.exe transport.');
    }

    try {
        $cfg = [];
        $cfg[] = 'url = "' . curlConfigEscape($url) . '"';
        $cfg[] = 'request = "' . curlConfigEscape($method) . '"';
        foreach ($headers as $header) {
            $cfg[] = 'header = "' . curlConfigEscape($header) . '"';
        }
        if ($method === 'POST') {
            foreach ($postFields as $key => $value) {
                $cfg[] = 'form = "' . curlConfigEscape((string)$key . '=' . (string)$value) . '"';
            }
        }

        if (@file_put_contents($configFile, implode(PHP_EOL, $cfg) . PHP_EOL) === false) {
            throw new RuntimeException('Unable to write curl config file.');
        }

        $cmd = implode(' ', [
            'curl.exe',
            '-sS',
            '--http1.1',
            '--config', escapeshellarg($configFile),
            '-D', escapeshellarg($headerFile),
            '-o', escapeshellarg($bodyFile),
            '--cookie', escapeshellarg($cookieFile),
            '--cookie-jar', escapeshellarg($cookieFile),
        ]);

        shell_exec($cmd);

        $rawHeaders = @file_get_contents($headerFile);
        $body = @file_get_contents($bodyFile);
        $status = extractLastStatusCode((string)$rawHeaders);
        if ($status <= 0) {
            throw new RuntimeException('Unable to parse HTTP status from curl.exe headers.');
        }

        return [$status, parseHeaderBlock((string)$rawHeaders), (string)$body];
    } finally {
        @unlink($headerFile);
        @unlink($bodyFile);
        @unlink($configFile);
    }
}

function curlConfigEscape(string $value): string
{
    $value = str_replace('\\', '\\\\', $value);
    $value = str_replace('"', '\\"', $value);
    $value = str_replace(["\r", "\n"], ['', ''], $value);
    return $value;
}

/**
 * @return array<string,string>
 */
function parseHeaderBlock(string $rawHeaders): array
{
    $parsed = [];
    if (trim($rawHeaders) === '') {
        return $parsed;
    }

    $blocks = preg_split("/\r\n\r\n|\n\n/", trim($rawHeaders));
    $last = is_array($blocks) && !empty($blocks) ? (string)end($blocks) : '';
    foreach (preg_split('/\r\n|\r|\n/', $last) as $line) {
        $parts = explode(':', $line, 2);
        if (count($parts) === 2) {
            $parsed[strtolower(trim($parts[0]))] = trim($parts[1]);
        }
    }

    return $parsed;
}

function extractLastStatusCode(string $rawHeaders): int
{
    $status = 0;
    if (trim($rawHeaders) === '') {
        return $status;
    }

    foreach (preg_split('/\r\n|\r|\n/', $rawHeaders) as $line) {
        if (preg_match('#^HTTP/\S+\s+(\d{3})#', $line, $m) === 1) {
            $status = (int)$m[1];
        }
    }

    return $status;
}

/**
 * @return array<string,mixed>
 */
function parseCarFoundHtml(string $html): array
{
    $out = [
        'title' => '',
        'fields' => [],
    ];

    if (trim($html) === '') {
        return $out;
    }

    libxml_use_internal_errors(true);
    $dom = new DOMDocument();
    if (!$dom->loadHTML($html)) {
        return $out;
    }

    $xpath = new DOMXPath($dom);

    $h3Nodes = $xpath->query('//h3');
    if ($h3Nodes !== false && $h3Nodes->length > 0) {
        $out['title'] = trim((string)$h3Nodes->item(0)?->textContent);
    }

    $dtNodes = $xpath->query('//dt');
    if ($dtNodes !== false) {
        foreach ($dtNodes as $dt) {
            $label = trim((string)$dt->textContent);
            if ($label === '') {
                continue;
            }

            $dd = $xpath->query('following-sibling::dd[1]', $dt)->item(0);
            $value = trim((string)($dd?->textContent ?? ''));
            if ($value !== '') {
                $out['fields'][$label] = $value;
            }
        }
    }

    return $out;
}

/**
 * @param array<string,mixed> $parsed
+ * @return array<string,mixed>
 */
function normalizeCarDetails(array $parsed, string $rego, string $state): array
{
    $title = trim((string)($parsed['title'] ?? ''));
    $fields = is_array($parsed['fields'] ?? null) ? $parsed['fields'] : [];

    $required = [
        'Registration plate' => 'registration_plate',
        'VIN' => 'vin',
        'State of issue' => 'state_of_issue',
        'Transmission' => 'transmission',
        'Build year' => 'build_year',
        'Fuel type' => 'fuel_type',
        'Engine' => 'engine',
        'Body type' => 'body_type',
    ];

    $missing = [];
    $car = [
        'title' => $title,
    ];

    foreach ($required as $label => $key) {
        $value = isset($fields[$label]) ? trim((string)$fields[$label]) : '';
        if ($value === '') {
            $missing[] = $label;
            continue;
        }
        $car[$key] = $key === 'build_year' ? (int)$value : $value;
    }

    if ($title === '') {
        $missing[] = 'title';
    }

    if (!empty($missing)) {
        throw new RuntimeException('Car details are incomplete from Carma response. Missing: ' . implode(', ', $missing));
    }

    if (strtoupper((string)$car['registration_plate']) !== strtoupper($rego)) {
        throw new RuntimeException('Carma returned a different registration plate than requested.');
    }

    if (strtoupper((string)$car['state_of_issue']) !== strtoupper($state)) {
        throw new RuntimeException('Carma returned a different state than requested.');
    }

    return $car;
}
