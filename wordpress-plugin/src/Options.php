<?php
namespace KosmosBridge;

defined( 'ABSPATH' ) || exit;

class Options {
	const SITE_UUID                 = 'kosmos_bridge_site_uuid';
	const SITE_SECRET               = 'kosmos_bridge_site_secret';
	const REGISTRATION_STATUS       = 'kosmos_bridge_registration_status';
	const REGISTRATION_MESSAGE      = 'kosmos_bridge_registration_message';
	const LAST_REGISTERED_AT        = 'kosmos_bridge_last_registered_at';
	const LAST_SUCCESS_AT           = 'kosmos_bridge_last_success_at';
	const LAST_REQUEST_ID           = 'kosmos_bridge_last_request_id';
	const SERVER_BASE_URL           = 'kosmos_bridge_server_base_url';
	const BRIDGE_VERSION            = '0.3.5';
	const DEFAULT_SERVER_BASE_URL   = 'https://kosmos-hub.31-70-92-95.sslip.io';

	/**
	 * @return string
	 */
	public static function get_site_uuid() {
		return (string) get_option( self::SITE_UUID, '' );
	}

	/**
	 * @return string
	 */
	public static function get_site_secret() {
		return (string) get_option( self::SITE_SECRET, '' );
	}

	/**
	 * @return string
	 */
	public static function get_server_base_url() {
		if ( defined( 'KOSMOS_HUB_BASE_URL' ) ) {
			return rtrim( (string) KOSMOS_HUB_BASE_URL, '/' );
		}

		$stored_url = rtrim( (string) get_option( self::SERVER_BASE_URL, '' ), '/' );
		if ( '' !== $stored_url ) {
			return $stored_url;
		}

		return self::DEFAULT_SERVER_BASE_URL;
	}

	/**
	 * @return string
	 */
	public static function get_registration_status() {
		return (string) get_option( self::REGISTRATION_STATUS, 'pending' );
	}

	/**
	 * @return string
	 */
	public static function get_registration_message() {
		return (string) get_option( self::REGISTRATION_MESSAGE, '' );
	}

	/**
	 * @return string
	 */
	public static function get_last_registered_at() {
		return (string) get_option( self::LAST_REGISTERED_AT, '' );
	}

	/**
	 * @return string
	 */
	public static function get_last_success_at() {
		return (string) get_option( self::LAST_SUCCESS_AT, '' );
	}

	/**
	 * @return string
	 */
	public static function get_last_request_id() {
		return (string) get_option( self::LAST_REQUEST_ID, '' );
	}

	/**
	 * @return string
	 */
	public static function get_bridge_version() {
		return self::BRIDGE_VERSION;
	}

	/**
	 * @return string
	 */
	public static function get_mcp_endpoint() {
		return rest_url( 'kosmos-bridge/v1/mcp' );
	}

	/**
	 * @param string $status Registration status.
	 * @param string $message Human-readable message.
	 * @param string $request_id Request id.
	 * @return void
	 */
	public static function set_registration_result( $status, $message, $request_id = '' ) {
		update_option( self::REGISTRATION_STATUS, (string) $status, false );
		update_option( self::REGISTRATION_MESSAGE, (string) $message, false );
		update_option( self::LAST_REGISTERED_AT, gmdate( 'c' ), false );

		if ( 'ok' === $status ) {
			update_option( self::LAST_SUCCESS_AT, gmdate( 'c' ), false );
		}

		if ( '' !== $request_id ) {
			update_option( self::LAST_REQUEST_ID, (string) $request_id, false );
		}
	}
}
