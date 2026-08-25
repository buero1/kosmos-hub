<?php
/**
 * Plugin Name:       Kosmos Bridge
 * Description:       Registers a WordPress site with kosmos-hub and prepares future MCP connectivity.
 * Version:           0.3.6
 * Requires at least: 5.8
 * Tested up to:      7.1
 * Requires PHP:      7.4
 * Author:            Kosmos
 * License:           GPL-2.0-or-later
 * Text Domain:       kosmos-bridge
 * Update URI:        https://plugins.kosmos-medien.de/kosmos-bridge/
 */

defined( 'ABSPATH' ) || exit;

define( 'KOSMOS_BRIDGE_PLUGIN_FILE', __FILE__ );
define( 'KOSMOS_BRIDGE_PLUGIN_DIR', __DIR__ );

if ( file_exists( __DIR__ . '/vendor/autoload.php' ) ) {
	require_once __DIR__ . '/vendor/autoload.php';
}

spl_autoload_register(
	static function ( $class ) {
		$prefix = 'KosmosBridge\\';
		if ( 0 !== strpos( $class, $prefix ) ) {
			return;
		}

		$relative = substr( $class, strlen( $prefix ) );
		$relative = str_replace( '\\', DIRECTORY_SEPARATOR, $relative );
		$file     = __DIR__ . '/src/' . $relative . '.php';

		if ( file_exists( $file ) ) {
			require_once $file;
		}
	}
);

\KosmosBridge\Plugin::boot( KOSMOS_BRIDGE_PLUGIN_FILE );
