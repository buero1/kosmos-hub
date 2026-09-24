<?php
namespace KosmosBridge\Registration;

use KosmosBridge\Options;

defined( 'ABSPATH' ) || exit;

class PayloadFactory {
	/**
	 * @param bool $heartbeat Whether this payload is a heartbeat.
	 * @return array
	 */
	public function make( $heartbeat, array $identity ) {
		$payload = array(
			'site_uuid'               => $identity['uuid'],
			'home_url'                => home_url( '/' ),
			'site_url'                => site_url( '/' ),
			'wordpress_version'       => get_bloginfo( 'version' ),
			'php_version'             => PHP_VERSION,
			'bridge_version'          => Options::get_bridge_version(),
			'mcp_endpoint'            => Options::get_mcp_endpoint(),
			'registration_timestamp'  => gmdate( 'c' ),
			'heartbeat'               => (bool) $heartbeat,
		);

		if ( ! $heartbeat ) {
			$payload['site_secret'] = $identity['secret'];
		}

		return $payload;
	}

}
