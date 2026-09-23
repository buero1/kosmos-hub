<?php
// Isolated WordPress contract harness: no network or customer data.
define( 'ABSPATH', __DIR__ );
$options = array();
$domain = 'https://template.example/';
$counter = 0;
$checks = 0;
$responses = array();
$requests = array();
function get_option( $key, $default = false ) { global $options; return $options[$key] ?? $default; }
function update_option( $key, $value, $autoload = false ) { global $options; $options[$key] = $value; return true; }
function add_option( $key, $value, $unused = '', $autoload = false ) { global $options; if ( isset($options[$key]) ) { return false; } $options[$key] = $value; return true; }
function delete_option( $key ) { global $options; unset($options[$key]); }
function wp_cache_delete( $key, $group ) {}
function wp_generate_uuid4() { global $counter; return sprintf('00000000-0000-4000-8000-%012d', ++$counter); }
function wp_generate_password( $length, ...$rest ) { return str_repeat('x', $length); }
function home_url( $path = '/' ) { global $domain; return $domain; }
function site_url( $path = '/' ) { return home_url(); }
function rest_url( $path ) { return home_url() . 'wp-json/' . $path; }
function wp_parse_url( $value, $component = -1 ) { return parse_url($value, $component); }
function get_bloginfo( $field ) { return '7.1'; }
function wp_json_encode( $value ) { return json_encode($value); }
function trailingslashit( $value ) { return rtrim($value, '/') . '/'; }
function __( $text, ...$rest ) { return $text; }
function wp_remote_post( $url, $args ) { global $responses, $requests; $requests[] = json_decode($args['body'], true); $response = array_shift($responses); return is_callable($response) ? $response(end($requests)) : $response; }
function wp_remote_retrieve_response_code( $response ) { return $response['code']; }
function wp_remote_retrieve_body( $response ) { return $response['body']; }
class WP_Error {
    private $code; private $message;
    function __construct($code, $message, ...$rest) { $this->code=$code; $this->message=$message; }
    function get_error_code() { return $this->code; }
    function get_error_message() { return $this->message; }
}
function is_wp_error($value) { return $value instanceof WP_Error; }
function check($condition, $message) { global $checks; ++$checks; if (!$condition) { throw new Exception($message); } }
require __DIR__ . '/../wordpress-plugin/src/Options.php';
require __DIR__ . '/../wordpress-plugin/src/Registration/SecretStore.php';
require __DIR__ . '/../wordpress-plugin/src/Registration/PayloadFactory.php';
require __DIR__ . '/../wordpress-plugin/src/Http/RegistrationClient.php';
require __DIR__ . '/../wordpress-plugin/src/Registration/Registrar.php';
use KosmosBridge\Options;
use KosmosBridge\Registration\SecretStore;
use KosmosBridge\Registration\Registrar;
use KosmosBridge\Http\RegistrationClient;

check(SecretStore::ensure_identity(), 'Fresh setup');
$original = get_option(Options::IDENTITY);
check($original['domain'] === 'template.example', 'Domain stored');
check(strlen($original['secret']) === 64, 'Random secret');
check(SecretStore::ensure_identity(), 'Repeated boot');
check(get_option(Options::IDENTITY) === $original, 'Stable boot identity');
$domain = 'http://www.template.example/';
SecretStore::ensure_identity();
check(get_option(Options::IDENTITY) === $original, 'Scheme/www do not rotate');
$template = $options;
$domain = 'https://copy.example/';
update_option(Options::LAST_SUCCESS_AT, 'yesterday');
SecretStore::ensure_identity();
$copy = get_option(Options::IDENTITY);
check($copy['uuid'] !== $original['uuid'], 'Copy gets own UUID');
check($copy['secret'] !== $original['secret'], 'Copy gets own key');
check(Options::get_last_success_at() === '', 'Copied success cleared');
$options = $template;
$domain = 'https://second-copy.example/';
SecretStore::ensure_identity();
check(Options::get_site_uuid() !== $copy['uuid'], 'Independent second copy');
check(Options::get_site_secret() !== $copy['secret'], 'Independent second key');
$options = $template;
$domain = 'https://template.example/';
check(SecretStore::ensure_identity(), 'Original survives');
check(get_option(Options::IDENTITY) === $original, 'Original unaffected');

$options = array(Options::SITE_UUID => 'legacy', Options::SITE_SECRET => 'old-key', Options::LAST_SUCCESS_AT => 'yesterday');
SecretStore::ensure_identity();
check(Options::get_site_uuid() === 'legacy', 'Existing site migration keeps identity');
check(Options::get_site_secret() === 'old-key', 'Existing site migration keeps key');
check(Options::get_last_success_at() === '', 'Legacy binding requests Hub check');
check(!isset($options[Options::SITE_SECRET]), 'Old key storage removed');
$success = function($payload) { return array('code'=>200, 'body'=>json_encode(array('site_uuid'=>$payload['site_uuid'], 'status'=>'verified'))); };
$responses = array(array('code'=>409, 'body'=>json_encode(array('detail'=>array('code'=>'bridge_domain_changed')))), $success);
check((new Registrar())->register(true), 'Legacy collision transparently bootstraps');
check(Options::get_site_uuid() !== 'legacy', 'Legacy collision rotates UUID');
check(Options::get_site_secret() !== 'old-key', 'Legacy collision rotates key');
check(end($requests)['heartbeat'] === false, 'Copied cron bootstraps');
check(isset(end($requests)['site_secret']), 'Bootstrap includes key');
$registered = get_option(Options::IDENTITY);
check(SecretStore::ensure_identity('legacy'), 'Concurrent legacy conflict is harmless');
check(get_option(Options::IDENTITY) === $registered, 'Conflict does not rotate twice');
$responses = array(array('code'=>401, 'body'=>json_encode(array('detail'=>'Invalid signature'))));
check(!(new Registrar())->register(false), 'Unrelated error stays an error');
check(get_option(Options::IDENTITY) === $registered, 'No identity churn on unrelated errors');
$responses = array(array('code'=>200, 'body'=>'<html>Login</html>'));
check(is_wp_error((new RegistrationClient())->post(array('site_uuid'=>'abc'))), 'HTML 200 is not successful registration');
$domain = 'https://third-copy.example/';
add_option(SecretStore::IDENTITY_LOCK, array('token'=>'other-request', 'expires'=>time()+120));
check(!SecretStore::ensure_identity(), 'Concurrent rotation fails closed');
check(get_option(Options::IDENTITY) === $registered, 'Busy lock does not expose new mixed credentials');
delete_option(SecretStore::IDENTITY_LOCK);
check(SecretStore::ensure_identity(), 'Retry after lock succeeds');
echo "Bridge identity contracts: $checks passed\n";
