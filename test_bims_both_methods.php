#!/usr/bin/env php
<?php
/**
 * BIMS Login Test Script - Prueba GET vs POST
 * 
 * Este script prueba ambos métodos HTTP para determinar
 * cuál acepta la API de BIMS.
 */

echo "\n╔════════════════════════════════════════╗\n";
echo "║  BIMS API - Comparación GET vs POST   ║\n";
echo "╚════════════════════════════════════════╝\n\n";

function prompt($message) {
    echo $message;
    $handle = fopen("php://stdin", "r");
    $line = fgets($handle);
    fclose($handle);
    return trim($line);
}

function testLogin($method, $url, $user, $passwordHash, $tenant) {
    echo "\n" . str_repeat("═", 60) . "\n";
    echo "🧪 PROBANDO MÉTODO: $method\n";
    echo str_repeat("═", 60) . "\n";
    
    $loginUrl = $url . "/users/login/";
    
    if ($method === 'GET') {
        // GET: parámetros en la URL
        $queryParams = http_build_query([
            'user' => $user,
            'password' => $passwordHash,
            'tenant' => $tenant
        ]);
        $fullUrl = $loginUrl . "?" . $queryParams;
        
        echo "🔗 URL: $fullUrl\n";
        
        $ch = curl_init($fullUrl);
        curl_setopt($ch, CURLOPT_HTTPGET, true);
        
    } else {
        // POST: parámetros en JSON body
        $postData = json_encode([
            'user' => $user,
            'password' => $passwordHash,
            'tenant' => $tenant
        ]);
        
        echo "🔗 URL: $loginUrl\n";
        echo "📦 Body: $postData\n";
        
        $ch = curl_init($loginUrl);
        curl_setopt($ch, CURLOPT_POST, true);
        curl_setopt($ch, CURLOPT_POSTFIELDS, $postData);
        curl_setopt($ch, CURLOPT_HTTPHEADER, [
            'Content-Type: application/json',
            'Content-Length: ' . strlen($postData)
        ]);
    }
    
    curl_setopt($ch, CURLOPT_RETURNTRANSFER, true);
    curl_setopt($ch, CURLOPT_TIMEOUT, 30);
    curl_setopt($ch, CURLOPT_HEADER, true);
    
    $response = curl_exec($ch);
    $httpCode = curl_getinfo($ch, CURLINFO_HTTP_CODE);
    $headerSize = curl_getinfo($ch, CURLINFO_HEADER_SIZE);
    $error = curl_error($ch);
    curl_close($ch);
    
    $body = substr($response, $headerSize);
    
    // Resultados
    echo "\n📊 RESULTADOS:\n";
    echo str_repeat("─", 60) . "\n";
    
    if ($error) {
        echo "❌ Error cURL: $error\n";
    }
    
    echo "📌 HTTP Status: ";
    if ($httpCode >= 200 && $httpCode < 300) {
        echo "✅ $httpCode (Exitoso)\n";
    } elseif ($httpCode >= 400 && $httpCode < 500) {
        echo "❌ $httpCode (Error del cliente)\n";
    } else {
        echo "⚠️  $httpCode\n";
    }
    
    echo "\n📥 Response Body:\n";
    if (empty($body)) {
        echo "⚠️  VACÍO (esto causa JSONDecodeError en Python)\n";
    } else {
        echo $body . "\n";
        
        $jsonData = json_decode($body, true);
        if (json_last_error() === JSON_ERROR_NONE) {
            echo "\n✅ JSON válido\n";
            echo "📄 JSON formateado:\n";
            echo json_encode($jsonData, JSON_PRETTY_PRINT) . "\n";
        } else {
            echo "\n❌ NO es JSON válido: " . json_last_error_msg() . "\n";
        }
    }
    
    return [
        'success' => ($httpCode >= 200 && $httpCode < 300),
        'http_code' => $httpCode,
        'is_json' => (json_last_error() === JSON_ERROR_NONE),
        'body' => $body
    ];
}

// Solicitar credenciales
$user = prompt("👤 Usuario BIMS: ");
$password = prompt("🔑 Password (texto plano): ");
$tenant = prompt("🏢 Tenant: ");
$url = prompt("🌐 URL API [https://bims.app/api]: ");

if (empty($url)) {
    $url = "https://bims.app/api";
}

$passwordHash = md5($password);

echo "\n" . str_repeat("─", 60) . "\n";
echo "📋 CREDENCIALES\n";
echo str_repeat("─", 60) . "\n";
echo "Usuario:      $user\n";
echo "Password MD5: $passwordHash\n";
echo "Tenant:       $tenant\n";
echo "URL Base:     $url\n";

// Probar ambos métodos
$resultGet = testLogin('GET', $url, $user, $passwordHash, $tenant);
$resultPost = testLogin('POST', $url, $user, $passwordHash, $tenant);

// Comparación final
echo "\n\n" . str_repeat("═", 60) . "\n";
echo "🏁 COMPARACIÓN FINAL\n";
echo str_repeat("═", 60) . "\n\n";

echo "┌─────────────┬──────────┬──────────┐\n";
echo "│   Método    │   GET    │   POST   │\n";
echo "├─────────────┼──────────┼──────────┤\n";

printf("│ HTTP Code   │   %-3s    │   %-3s    │\n", 
    $resultGet['http_code'], 
    $resultPost['http_code']
);

printf("│ Exitoso     │    %s    │    %s    │\n",
    $resultGet['success'] ? '✅' : '❌',
    $resultPost['success'] ? '✅' : '❌'
);

printf("│ JSON válido │    %s    │    %s    │\n",
    $resultGet['is_json'] ? '✅' : '❌',
    $resultPost['is_json'] ? '✅' : '❌'
);

echo "└─────────────┴──────────┴──────────┘\n\n";

// Recomendación
echo "💡 RECOMENDACIÓN:\n";
echo str_repeat("─", 60) . "\n";

if ($resultGet['success'] && !$resultPost['success']) {
    echo "✅ Usar GET - Solo GET funciona correctamente\n";
    echo "   El código Python debe cambiarse de POST a GET\n";
} elseif ($resultPost['success'] && !$resultGet['success']) {
    echo "✅ Usar POST - Solo POST funciona correctamente\n";
    echo "   El código Python está correcto usando POST\n";
} elseif ($resultGet['success'] && $resultPost['success']) {
    echo "✅ Ambos métodos funcionan - BIMS acepta GET y POST\n";
    echo "   El problema debe estar en otro lado (headers, formato, etc.)\n";
} else {
    echo "❌ Ningún método funciona - Revisar credenciales o conectividad\n";
}

echo "\n";
