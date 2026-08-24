<?php
namespace KosmosBridge\Registration;

use KosmosBridge\Options;

defined( 'ABSPATH' ) || exit;

class PayloadFactory {
	/**
	 * @param bool $heartbeat Whether this payload is a heartbeat.
	 * @return array
	 */
	public function make( $heartbeat = false ) {
		$payload = array(
			'site_uuid'               => Options::get_site_uuid(),
			'home_url'                => home_url( '/' ),
			'site_url'                => site_url( '/' ),
			'wordpress_version'       => get_bloginfo( 'version' ),
			'php_version'             => PHP_VERSION,
			'bridge_version'          => Options::get_bridge_version(),
			'mcp_endpoint'            => Options::get_mcp_endpoint(),
			'registration_timestamp'  => gmdate( 'c' ),
			'heartbeat'               => (bool) $heartbeat,
		);

		if ( self::should_include_secret( $heartbeat ) ) {
			$payload['site_secret'] = Options::get_site_secret();
		}

		return $payload;
	}

	/**
	 * @param bool $heartbeat Whether this payload is a heartbeat.
	 * @return bool
	 */
	private static function should_include_secret( $heartbeat ) {
		if ( $heartbeat ) {
			return false;
		}

		return '' === Options::get_last_success_at() || 'ok' !== Options::get_registration_status();
	}
}
