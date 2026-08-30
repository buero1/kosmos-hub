<?php
namespace KosmosBridge;

use WP_Error;

defined( 'ABSPATH' ) || exit;

/**
 * Issues one-time, short-lived browser handoffs from the Hub to wp-admin.
 * The only long-lived credential stays in the signed Hub-to-Bridge channel.
 */
class AdminLaunch {
	const ABILITY_NAME              = 'kosmos-bridge/prepare-admin-launch';
	const ACCESS_USER_OPTION        = 'kosmos_bridge_hub_access_user_id';
	const ACCESS_USER_META          = 'kosmos_bridge_hub_access_user';
	const ACCESS_USER_LOGIN         = 'kosmos-hub-access';
	const WHITE_LABEL_CMS_PLUGIN    = 'white-label-cms/wlcms-plugin.php';
	const WHITE_LABEL_CMS_OPTIONS   = 'wlcms_options';
	const DESTINATION_DASHBOARD     = 'dashboard';
	const DESTINATION_PLUGINS       = 'plugins';
	const LAUNCH_QUERY_ARGUMENT     = 'kosmos_admin_launch';
	const LAUNCH_OPTION_PREFIX      = 'kosmos_bridge_admin_launch_';
	const LAUNCH_TTL                = 60;
	const TOKEN_ID_LENGTH           = 24;
	const TOKEN_SECRET_LENGTH       = 64;

	/**
	 * @return array|WP_Error
	 */
	public static function execute_prepare_admin_launch( $input = array() ) {
		$destination = self::normalize_destination( $input );
		if ( is_wp_error( $destination ) ) {
			return $destination;
		}

		$access_user_created = 0 === (int) get_option( self::ACCESS_USER_OPTION, 0 ) && ! self::has_reusable_default_access_user();
		$user = self::get_or_create_access_user();
		if ( is_wp_error( $user ) ) {
			return $user;
		}
		$white_label_access_granted = self::grant_white_label_cms_access( $user );

		$launch = self::create_launch_ticket( (int) $user->ID, $destination );
		if ( is_wp_error( $launch ) ) {
			return $launch;
		}

		return array(
			'launch_url'          => add_query_arg( self::LAUNCH_QUERY_ARGUMENT, $launch['token'], home_url( '/' ) ),
			'expires_at'          => gmdate( 'c', $launch['expires_at'] ),
			'access_user_created' => $access_user_created,
			'white_label_access_granted' => $white_label_access_granted,
		);
	}

	/**
	 * Handles the browser side of a one-time ticket. This is deliberately not
	 * a REST endpoint, because site security plugins often restrict REST access.
	 *
	 * @return void
	 */
	public static function maybe_handle_launch() {
		if ( ! isset( $_GET[ self::LAUNCH_QUERY_ARGUMENT ] ) ) {
			return;
		}

		$token   = is_string( $_GET[ self::LAUNCH_QUERY_ARGUMENT ] ) ? wp_unslash( $_GET[ self::LAUNCH_QUERY_ARGUMENT ] ) : '';
		$launch = self::consume_launch_ticket( $token );
		if ( ! is_array( $launch ) ) {
			self::deny_launch();
		}
		$user_id = (int) $launch['user_id'];

		$user = get_user_by( 'id', $user_id );
		if ( ! $user instanceof \WP_User || ! self::is_administrator_user( $user ) ) {
			self::deny_launch();
		}

		wp_set_current_user( $user_id );
		wp_set_auth_cookie( $user_id, false, is_ssl() );
		do_action( 'wp_login', $user->user_login, $user );

		nocache_headers();
		header( 'Referrer-Policy: no-referrer' );
		wp_safe_redirect( self::destination_url( (string) $launch['destination'] ) );
		exit;
	}

	/**
	 * The Hub can select only the dashboard or the plugin management screen.
	 * Persisting a named destination inside the one-time ticket avoids trusting
	 * an arbitrary redirect path from the browser.
	 *
	 * @param mixed $input Ability input.
	 * @return string|WP_Error
	 */
	private static function normalize_destination( $input ) {
		$destination = is_array( $input ) && isset( $input['destination'] ) ? (string) $input['destination'] : self::DESTINATION_DASHBOARD;
		if ( in_array( $destination, array( self::DESTINATION_DASHBOARD, self::DESTINATION_PLUGINS ), true ) ) {
			return $destination;
		}

		return new WP_Error(
			'kosmos_bridge_admin_launch_destination_invalid',
			'The requested WordPress admin destination is not supported.',
			array( 'status' => 422 )
		);
	}

	/**
	 * @param string $destination Validated ticket destination.
	 * @return string
	 */
	private static function destination_url( $destination ) {
		return self::DESTINATION_PLUGINS === $destination ? admin_url( 'plugins.php' ) : admin_url();
	}

	/**
	 * @return WP_User|WP_Error
	 */
	private static function get_or_create_access_user() {
		$stored_user_id = (int) get_option( self::ACCESS_USER_OPTION, 0 );
		if ( $stored_user_id > 0 ) {
			$stored_user = get_user_by( 'id', $stored_user_id );
			if ( ! $stored_user instanceof \WP_User ) {
				return new WP_Error(
					'kosmos_bridge_admin_access_user_missing',
					'The configured Kosmos Hub access user no longer exists.',
					array( 'status' => 409 )
				);
			}
			if ( ! self::is_administrator_user( $stored_user ) ) {
				return new WP_Error(
					'kosmos_bridge_admin_access_user_not_administrator',
					'The configured Kosmos Hub access user no longer has administrator permission.',
					array( 'status' => 409 )
				);
			}

			return $stored_user;
		}

		$existing_user = self::get_reusable_default_access_user();
		if ( $existing_user instanceof \WP_User ) {
			return self::remember_access_user( $existing_user );
		}

		$login = self::next_available_access_login();
		if ( '' === $login ) {
			return new WP_Error(
				'kosmos_bridge_admin_access_user_unavailable',
				'WordPress could not allocate a dedicated Kosmos Hub access user.',
				array( 'status' => 500 )
			);
		}

		$user_id = wp_insert_user(
			array(
				'user_login'   => $login,
				'user_pass'    => wp_generate_password( 64, true, true ),
				'display_name' => 'Kosmos Hub',
				'role'         => 'administrator',
			)
		);
		if ( is_wp_error( $user_id ) ) {
			return $user_id;
		}

		$user = get_user_by( 'id', (int) $user_id );
		if ( ! $user instanceof \WP_User || ! self::is_administrator_user( $user ) ) {
			return new WP_Error(
				'kosmos_bridge_admin_access_user_unverified',
				'WordPress could not verify the dedicated Kosmos Hub administrator.',
				array( 'status' => 500 )
			);
		}

		return self::remember_access_user( $user );
	}

	/**
	 * Some security plugins alter capability checks while an authenticated Bridge
	 * request is being processed. The account's explicit WordPress role is the
	 * stable fact we need to verify before issuing a browser ticket.
	 *
	 * @param WP_User $user WordPress user to inspect.
	 * @return bool
	 */
	private static function is_administrator_user( \WP_User $user ) {
		return in_array( 'administrator', (array) $user->roles, true );
	}

	/**
	 * Reuses a partially created account from an earlier interrupted launch.
	 *
	 * @return WP_User|false
	 */
	private static function get_reusable_default_access_user() {
		$user = get_user_by( 'login', self::ACCESS_USER_LOGIN );
		return $user instanceof \WP_User && self::is_administrator_user( $user ) ? $user : false;
	}

	/**
	 * @return bool
	 */
	private static function has_reusable_default_access_user() {
		return self::get_reusable_default_access_user() instanceof \WP_User;
	}

	/**
	 * @param WP_User $user Verified dedicated access user.
	 * @return WP_User
	 */
	private static function remember_access_user( \WP_User $user ) {
		update_user_meta( $user->ID, self::ACCESS_USER_META, '1' );
		update_option( self::ACCESS_USER_OPTION, (int) $user->ID, false );
		return $user;
	}

	/**
	 * White Label CMS stores the administrators allowed to see its full backend
	 * as WordPress user IDs. Add only the dedicated Hub account and leave every
	 * other White Label setting unchanged.
	 *
	 * @param WP_User $user Verified dedicated access user.
	 * @return bool Whether the user was newly added to White Label CMS access.
	 */
	private static function grant_white_label_cms_access( \WP_User $user ) {
		if ( ! self::is_white_label_cms_active() ) {
			return false;
		}

		$settings = get_option( self::WHITE_LABEL_CMS_OPTIONS, array() );
		if ( ! is_array( $settings ) || empty( $settings['enable_wlcms_admin'] ) ) {
			return false;
		}

		$administrator_ids = array_values( array_unique( array_filter( array_map( 'absint', (array) ( $settings['wlcms_admin'] ?? array() ) ) ) ) );
		if ( in_array( (int) $user->ID, $administrator_ids, true ) ) {
			return false;
		}

		$administrator_ids[]      = (int) $user->ID;
		$settings['wlcms_admin'] = $administrator_ids;
		if ( ! update_option( self::WHITE_LABEL_CMS_OPTIONS, $settings, false ) ) {
			return false;
		}

		$stored_settings = get_option( self::WHITE_LABEL_CMS_OPTIONS, array() );
		$stored_ids      = is_array( $stored_settings ) ? array_map( 'absint', (array) ( $stored_settings['wlcms_admin'] ?? array() ) ) : array();
		return in_array( (int) $user->ID, $stored_ids, true );
	}

	/**
	 * Avoid loading White Label CMS classes during an authenticated Bridge call.
	 * The plugin's active-state data is enough to decide whether its option can
	 * safely be updated.
	 *
	 * @return bool
	 */
	private static function is_white_label_cms_active() {
		if ( defined( 'WLCMS_VERSION' ) ) {
			return true;
		}

		$active_plugins = (array) get_option( 'active_plugins', array() );
		if ( in_array( self::WHITE_LABEL_CMS_PLUGIN, $active_plugins, true ) ) {
			return true;
		}

		if ( is_multisite() ) {
			$network_active = (array) get_site_option( 'active_sitewide_plugins', array() );
			return isset( $network_active[ self::WHITE_LABEL_CMS_PLUGIN ] );
		}

		return false;
	}

	/**
	 * @return string
	 */
	private static function next_available_access_login() {
		for ( $suffix = 1; $suffix <= 20; $suffix++ ) {
			$login = 1 === $suffix ? self::ACCESS_USER_LOGIN : self::ACCESS_USER_LOGIN . '-' . $suffix;
			if ( ! username_exists( $login ) ) {
				return $login;
			}
		}

		return '';
	}

	/**
	 * @param int    $user_id WordPress administrator ID.
	 * @param string $destination Validated admin destination.
	 * @return array|WP_Error
	 */
	private static function create_launch_ticket( $user_id, $destination ) {
		self::delete_expired_launch_tickets();

		try {
			$ticket_id = bin2hex( random_bytes( self::TOKEN_ID_LENGTH / 2 ) );
			$secret    = bin2hex( random_bytes( self::TOKEN_SECRET_LENGTH / 2 ) );
		} catch ( \Throwable $exception ) {
			return new WP_Error(
				'kosmos_bridge_admin_launch_entropy_failed',
				'WordPress could not create a secure one-time admin launch.',
				array( 'status' => 500 )
			);
		}

		$expires_at = time() + self::LAUNCH_TTL;
		$payload    = wp_json_encode(
			array(
				'user_id'     => $user_id,
				'destination' => $destination,
				'secret_hash' => hash( 'sha256', $secret ),
				'expires_at'  => $expires_at,
			)
		);
		if ( ! is_string( $payload ) || ! add_option( self::launch_option_name( $ticket_id ), $payload, '', 'no' ) ) {
			return new WP_Error(
				'kosmos_bridge_admin_launch_store_failed',
				'WordPress could not prepare the one-time admin launch.',
				array( 'status' => 500 )
			);
		}

		return array(
			'token'      => $ticket_id . '.' . $secret,
			'expires_at' => $expires_at,
		);
	}

	/**
	 * @param string $token Browser-provided ticket.
	 * @return array|false
	 */
	private static function consume_launch_ticket( $token ) {
		if ( ! is_string( $token ) || ! preg_match( '/^([a-f0-9]{24})\.([a-f0-9]{64})$/', $token, $matches ) ) {
			return false;
		}

		global $wpdb;
		$option_name = self::launch_option_name( $matches[1] );
		$raw_payload = $wpdb->get_var( $wpdb->prepare( "SELECT option_value FROM {$wpdb->options} WHERE option_name = %s", $option_name ) );
		if ( ! is_string( $raw_payload ) || '' === $raw_payload ) {
			return false;
		}

		$payload = json_decode( $raw_payload, true );
		if ( ! is_array( $payload ) ) {
			return false;
		}

		$expected_secret_hash = isset( $payload['secret_hash'] ) ? (string) $payload['secret_hash'] : '';
		$expires_at           = isset( $payload['expires_at'] ) ? (int) $payload['expires_at'] : 0;
		$user_id              = isset( $payload['user_id'] ) ? (int) $payload['user_id'] : 0;
		if ( $expires_at < time() || $user_id <= 0 || ! hash_equals( $expected_secret_hash, hash( 'sha256', $matches[2] ) ) ) {
			return false;
		}

		$deleted = $wpdb->query(
			$wpdb->prepare(
				"DELETE FROM {$wpdb->options} WHERE option_name = %s AND option_value = %s",
				$option_name,
				$raw_payload
			)
		);
		if ( 1 !== (int) $deleted ) {
			return false;
		}

		$destination = isset( $payload['destination'] ) ? (string) $payload['destination'] : self::DESTINATION_DASHBOARD;
		if ( ! in_array( $destination, array( self::DESTINATION_DASHBOARD, self::DESTINATION_PLUGINS ), true ) ) {
			$destination = self::DESTINATION_DASHBOARD;
		}

		return array(
			'user_id'     => $user_id,
			'destination' => $destination,
		);
	}

	/**
	 * @param string $ticket_id Random ticket identifier.
	 * @return string
	 */
	private static function launch_option_name( $ticket_id ) {
		return self::LAUNCH_OPTION_PREFIX . hash( 'sha256', $ticket_id );
	}

	/**
	 * Remove unconsumed expired tickets while issuing a new one. Consumed
	 * tickets are deleted atomically by consume_launch_ticket().
	 *
	 * @return void
	 */
	private static function delete_expired_launch_tickets() {
		global $wpdb;
		$rows = $wpdb->get_results(
			$wpdb->prepare(
				"SELECT option_name, option_value FROM {$wpdb->options} WHERE option_name LIKE %s",
				self::LAUNCH_OPTION_PREFIX . '%'
			),
			ARRAY_A
		);
		if ( ! is_array( $rows ) ) {
			return;
		}

		$now = time();
		foreach ( $rows as $row ) {
			$option_name = isset( $row['option_name'] ) ? (string) $row['option_name'] : '';
			$option_value = isset( $row['option_value'] ) ? (string) $row['option_value'] : '';
			$payload = json_decode( $option_value, true );
			$expires_at = is_array( $payload ) && isset( $payload['expires_at'] ) ? (int) $payload['expires_at'] : 0;
			if ( '' !== $option_name && $expires_at < $now ) {
				$wpdb->query(
					$wpdb->prepare(
						"DELETE FROM {$wpdb->options} WHERE option_name = %s AND option_value = %s",
						$option_name,
						$option_value
					)
				);
			}
		}
	}

	/**
	 * @return void
	 */
	private static function deny_launch() {
		nocache_headers();
		header( 'Referrer-Policy: no-referrer' );
		wp_die( esc_html__( 'This WordPress admin access link is invalid or has expired.', 'kosmos-bridge' ), '', array( 'response' => 403 ) );
	}
}
