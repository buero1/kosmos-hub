<?php
namespace KosmosBridge;

use KosmosBridge\Registration\RegistrationState;
use KosmosBridge\Registration\SecretStore;

defined( 'ABSPATH' ) || exit;

class Options {
	const SITE_UUID                 = 'kosmos_bridge_site_uuid';
	const SITE_SECRET               = 'kosmos_bridge_site_secret';
	const IDENTITY                  = 'kosmos_bridge_domain_identity';
	const REGISTRATION_STATUS       = 'kosmos_bridge_registration_status';
	const REGISTRATION_MESSAGE      = 'kosmos_bridge_registration_message';
	const LAST_REGISTERED_AT        = 'kosmos_bridge_last_registered_at';
	const LAST_SUCCESS_AT           = 'kosmos_bridge_last_success_at';
	const LAST_REQUEST_ID           = 'kosmos_bridge_last_request_id';
	const SERVER_BASE_URL           = 'kosmos_bridge_server_base_url';
	const BRIDGE_VERSION            = '0.3.69';
	const DEFAULT_SERVER_BASE_URL   = 'https://kosmos-hub.31-70-92-95.sslip.io';

	/**
	 * @return string
	 */
	public static function get_site_uuid() {
		$identity = SecretStore::get_identity();
		return isset( $identity['uuid'] ) ? $identity['uuid'] : '';
	}

	/**
	 * @return string
	 */
	public static function get_site_secret() {
		$identity = SecretStore::get_identity();
		return isset( $identity['secret'] ) ? $identity['secret'] : '';
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
		$state = RegistrationState::current();
		return isset( $state['status'] ) ? (string) $state['status'] : 'pending';
	}

	/**
	 * @return string
	 */
	public static function get_registration_message() {
		$state = RegistrationState::current();
		return isset( $state['message'] ) ? (string) $state['message'] : 'Current identity requires confirmation by the Hub.';
	}

	/**
	 * @return string
	 */
	public static function get_last_registered_at() {
		$state = RegistrationState::current();
		return isset( $state['last_attempt_at'] ) ? (string) $state['last_attempt_at'] : '';
	}

	/**
	 * @return string
	 */
	public static function get_last_success_at() {
		$state = RegistrationState::current();
		return isset( $state['last_success_at'] ) ? (string) $state['last_success_at'] : '';
	}

	/**
	 * @return string
	 */
	public static function get_last_request_id() {
		$state = RegistrationState::current();
		return isset( $state['request_id'] ) ? (string) $state['request_id'] : '';
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

}
