#!/usr/bin/env php
<?php
/**
 * BIMS Login Test Script
 * 
 * Este script permite probar el login a BIMS API manualmente
 * para diagnosticar problemas de autenticación.
 * 
 * Uso: php test_bims_login.php
 */

echo "\n╔════════════════════════════════════════╗\n";
echo "║   BIMS API Login Test - Diagnóstico   ║\n";
echo "╚════════════════════════════════════════╝\n\n";

// Función para leer input del usuario
function prompt($message) {
    echo $message;
    $handle = fopen("php://stdin", "r");
    $line = fgets($handle);
    fclose($handle);
    return trim($line);
}

// Solicitar credenciales
$user = prompt("👤 Usuario BIMS: ");
$password = prompt("🔑 Password (texto plano): ");
$tenant = prompt("🏢 Tenant: ");
$url = prompt("🌐 URL API [https://bims.app/api]: ");

// Usar URL por defecto si está vacío
if (empty($url)) {
    $url = "https://bims.app/api";
}

echo "\n" . str_repeat("─", 50) . "\n";
echo "📋 DATOS DE LA PETICIÓN\n";
echo str_repeat("─", 50) . "\n";

// Generar hash MD5 del password
$passwordHash = md5($password);

echo "Usuario:       $user\n";
echo "Password:      " . str_repeat("*", strlen($password)) . "\n";
echo "Password MD5:  $passwordHash\n";
echo "Tenant:        $tenant\n";
echo "URL Base:      $url\n";
echo "Endpoint:      $url/users/login/\n";

echo "\n" . str_repeat("─", 50) . "\n";
echo "🚀 ENVIANDO PETICIÓN...\n";
echo str_repeat("─", 50) . "\n\n";

// Preparar datos para la petición (BIMS usa POST con query params en URL)
$queryParams = http_build_query([
    'user' => $user,
    'password' => $passwordHash,
    'tenant' => $tenant
]);
$loginUrl = $url . "/users/login/?" . $queryParams;

echo "🔗 URL Completa: $loginUrl\n";
echo "📤 Método HTTP: POST\n";

// Configurar cURL con POST (parámetros en query string, no en body)
$ch = curl_init($loginUrl);
curl_setopt($ch, CURLOPT_RETURNTRANSFER, true);
curl_setopt($ch, CURLOPT_POST, true);  // Usar POST
curl_setopt($ch, CURLOPT_HTTPHEADER, [
    'User-Agent: Mozilla/5.0 (compatible; MuciIntegrador/1.0; +https://muci.org)',
    'Accept: application/json, text/plain, */*',
    'Origin: https://muci.org',
    'Referer: https://muci.org/',
    'Accept-Language: es-ES,es;q=0.9,en;q=0.8',
    'Accept-Encoding: gzip, deflate, br'
]);
curl_setopt($ch, CURLOPT_TIMEOUT, 30);
curl_setopt($ch, CURLOPT_VERBOSE, false);
curl_setopt($ch, CURLOPT_HEADER, true);

// Ejecutar petición
$response = curl_exec($ch);
$httpCode = curl_getinfo($ch, CURLINFO_HTTP_CODE);
$headerSize = curl_getinfo($ch, CURLINFO_HEADER_SIZE);
$error = curl_error($ch);
curl_close($ch);

// Separar headers y body
$headers = substr($response, 0, $headerSize);
$body = substr($response, $headerSize);

// Mostrar resultados
echo "📊 RESULTADO DE LA PETICIÓN\n";
echo str_repeat("═", 50) . "\n\n";

if ($error) {
    echo "❌ ERROR DE CURL:\n";
    echo "   $error\n\n";
}

echo "📌 HTTP Status Code: ";
if ($httpCode >= 200 && $httpCode < 300) {
    echo "✅ $httpCode\n";
} else {
    echo "❌ $httpCode\n";
}

echo "\n" . str_repeat("─", 50) . "\n";
echo "📤 HEADERS DE RESPUESTA\n";
echo str_repeat("─", 50) . "\n";
echo $headers . "\n";

echo str_repeat("─", 50) . "\n";
echo "📥 BODY DE RESPUESTA\n";
echo str_repeat("─", 50) . "\n";

if (empty($body)) {
    echo "⚠️  RESPUESTA VACÍA (esto causa el JSONDecodeError)\n\n";
} else {
    echo $body . "\n\n";
    
    // Intentar decodificar JSON
    echo str_repeat("─", 50) . "\n";
    echo "🔍 ANÁLISIS DEL RESPONSE\n";
    echo str_repeat("─", 50) . "\n";
    
    $jsonData = json_decode($body, true);
    
    if (json_last_error() === JSON_ERROR_NONE) {
        echo "✅ El response es JSON válido\n\n";
        echo "📄 JSON Formateado:\n";
        echo json_encode($jsonData, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES) . "\n\n";
        
        if (isset($jsonData['status'])) {
            if ($jsonData['status'] === 'ok') {
                echo "✅ Status: OK - Login exitoso\n";
                if (isset($jsonData['data']['Session']['id'])) {
                    $sessionId = $jsonData['data']['Session']['id'];
                    echo "🎟️  Session ID: $sessionId\n";
                }
            } else {
                echo "❌ Status: " . $jsonData['status'] . "\n";
                if (isset($jsonData['message'])) {
                    echo "💬 Mensaje: " . $jsonData['message'] . "\n";
                }
            }
        }
    } else {
        echo "❌ El response NO es JSON válido\n";
        echo "🔴 Error JSON: " . json_last_error_msg() . "\n";
        echo "⚠️  Esto es lo que causa el error 'JSONDecodeError' en Python\n\n";
        
        // Detectar tipo de contenido
        if (strpos($body, '<!DOCTYPE') !== false || strpos($body, '<html') !== false) {
            echo "📄 Tipo detectado: HTML (probablemente página de error)\n";
        } else {
            echo "📄 Tipo detectado: Texto plano o desconocido\n";
        }
    }
}

echo "\n" . str_repeat("═", 50) . "\n";
echo "🏁 DIAGNÓSTICO COMPLETO\n";
echo str_repeat("═", 50) . "\n\n";
