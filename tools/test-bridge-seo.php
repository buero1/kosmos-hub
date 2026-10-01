<?php
define( 'ABSPATH', __DIR__ . '/' );
define( 'WPSEO_VERSION', '26.0' );
$admin = true;
$failed_write = '';
$meta = array(
	10 => array( '_yoast_wpseo_title' => 'Alt', '_yoast_wpseo_metadesc' => 'Altbeschreibung', '_yoast_wpseo_meta-robots-noindex' => '' ),
	11 => array( '_yoast_wpseo_title' => '', '_yoast_wpseo_metadesc' => '', '_yoast_wpseo_meta-robots-noindex' => '1' ),
);
$posts = array(
	10 => (object) array( 'ID' => 10, 'post_title' => 'Startseite', 'post_name' => 'start', 'post_content' => '<h1>Webdesign</h1><p>Klare Websites.</p>', 'post_modified_gmt' => '2026-10-01 08:00:00', 'post_type' => 'page', 'post_status' => 'publish' ),
	11 => (object) array( 'ID' => 11, 'post_title' => 'Impressum', 'post_name' => 'impressum', 'post_content' => '<p>Anbieter</p>', 'post_modified_gmt' => '2026-10-01 08:00:00', 'post_type' => 'page', 'post_status' => 'publish' ),
);
class WP_Error { public $code; public $message; public $data; public function __construct( $code, $message, $data = array() ) { $this->code = $code; $this->message = $message; $this->data = $data; } }
eval( 'namespace KosmosBridge\\Security; class SiteAuth { public static function is_authenticated() { return false; } }' );
function current_user_can( $cap ) { return $GLOBALS['admin']; }
function get_posts( $args ) { return array_values( $GLOBALS['posts'] ); }
function get_option( $name, $default = false ) { return 'page_on_front' === $name ? 10 : $default; }
function get_bloginfo( $name ) { return array( 'name' => 'Test', 'language' => 'de-DE', 'charset' => 'UTF-8' )[ $name ] ?? ''; }
function get_the_title( $post ) { return $post->post_title; }
function get_permalink( $post ) { return 'https://example.test/' . $post->post_name . '/'; }
function get_post_meta( $id, $key, $single = false ) { return $GLOBALS['meta'][ $id ][ $key ] ?? ''; }
function update_post_meta( $id, $key, $value ) {
	if ( $GLOBALS['failed_write'] === $id . ':' . $key ) {
		return false;
	}
	$GLOBALS['meta'][ $id ][ $key ] = $value;
	return true;
}
function get_post( $id ) { return $GLOBALS['posts'][ $id ] ?? null; }
function clean_post_cache( $id ) {}
function wp_strip_all_tags( $html, $remove_breaks = false ) { return strip_tags( $html ); }
function strip_shortcodes( $html ) { return $html; }
function is_wp_error( $value ) { return $value instanceof WP_Error; }
function check( $value, $message ) { if ( ! $value ) { throw new Exception( $message ); } }
require_once __DIR__ . '/../wordpress-plugin/src/Abilities/SeoMetadata.php';
use KosmosBridge\Abilities\SeoMetadata;

$read = SeoMetadata::read( array() );
check( $read['yoast_active'] && count( $read['pages'] ) === 2, 'Read published pages and Yoast state' );
check( $read['pages'][0]['id'] === 10 && $read['pages'][0]['is_front_page'], 'Front page sorts first' );
check( ! $read['pages'][1]['indexable'], 'Noindex is reported' );
$revision = $read['pages'][0]['revision'];
$changed = SeoMetadata::write( array( 'pages' => array( array( 'id' => 10, 'title' => 'Neuer Titel',
	'description' => 'Neue Beschreibung', 'revision' => $revision ) ) ) );
check( $changed['changed'] === 1 && $meta[10]['_yoast_wpseo_title'] === 'Neuer Titel', 'Confirmed metadata is written' );
$before = $meta;
$stale = SeoMetadata::write( array( 'pages' => array(
	array( 'id' => 10, 'title' => 'Nicht schreiben', 'description' => 'Nicht schreiben', 'revision' => $revision ),
	array( 'id' => 11, 'title' => 'Auch nicht', 'description' => 'Auch nicht', 'revision' => $read['pages'][1]['revision'] ),
) ) );
check( $stale instanceof WP_Error && $stale->code === 'KOSMOS_BRIDGE_SEO_CONFLICT' && $meta === $before, 'Conflict prevents every write' );
$fresh = SeoMetadata::read( array() );
$before = $meta;
$failed_write = '11:_yoast_wpseo_metadesc';
$partial = SeoMetadata::write( array( 'pages' => array(
	array( 'id' => 10, 'title' => 'Erste Seite', 'description' => 'Erste Beschreibung', 'revision' => $fresh['pages'][0]['revision'] ),
	array( 'id' => 11, 'title' => 'Zweite Seite', 'description' => 'Zweite Beschreibung', 'revision' => $fresh['pages'][1]['revision'] ),
) ) );
$failed_write = '';
check( $partial instanceof WP_Error && $partial->code === 'KOSMOS_BRIDGE_SEO_WRITE_FAILED' && $meta === $before, 'Unconfirmed batch write restores every original value' );
$admin = false;
check( SeoMetadata::write( array( 'pages' => array() ) ) instanceof WP_Error, 'Anonymous caller is rejected' );
echo "Bridge SEO metadata contracts passed.\n";
