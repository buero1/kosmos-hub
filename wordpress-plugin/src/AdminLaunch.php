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
	const LAUNCH_QUERY_ARGUMENT     = 'kosmos_admin_launch';
	const LAUNCH_OPTION_PREFIX      = 'kosmos_bridge_admin_launch_';
	const LAUNCH_TTL                = 60;
	const TOKEN_ID_LENGTH           = 24;
	const TOKEN_SECRET_LENGTH       = 64;

	/**
	 * @return array|WP_Error
	 */
	public static function execute_prepare_admin_launch() {
		$access_user_created = 0 === (int) get_option( self::ACCESS_USER_OPTION, 0 ) && ! self::has_reusable_default_access_user();
		$user = self::get_or_create_access_user();
		if ( is_wp_error( $user ) ) {
			return $user;
		}

		$launch = self::create_launch_ticket( (int) $user->ID );
		if ( is_wp_error( $launch ) ) {
			return $launch;
		}

		return array(
			'launch_url'          => add_query_arg( self::LAUNCH_QUERY_ARGUMENT, $launch['token'], home_url( '/' ) ),
			'expires_at'          => gmdate( 'c', $launch['expires_at'] ),
			'access_user_created' => $access_user_created,
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
		$user_id = self::consume_launch_ticket( $token );
		if ( $user_id <= 0 ) {
			self::deny_launch();
		}

		$user = get_user_by( 'id', $user_id );
		if ( ! $user instanceof \WP_User || ! self::is_administrator_user( $user ) ) {
			self::deny_launch();
		}

		wp_set_current_user( $user_id );
		wp_set_auth_cookie( $user_id, false, is_ssl() );
		do_action( 'wp_login', $user->user_login, $user );

		nocache_headers();
		header( 'Referrer-Policy: no-referrer' );
		wp_safe_redirect( admin_url() );
		exit;
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
	 * @param int $user_id WordPress administrator ID.
	 * @return array|WP_Error
	 */
	private static function create_launch_ticket( $user_id ) {
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
	 * @return int
	 */
	private static function consume_launch_ticket( $token ) {
		if ( ! is_string( $token ) || ! preg_match( '/^([a-f0-9]{24})\.([a-f0-9]{64})$/', $token, $matches ) ) {
			return 0;
		}

		global $wpdb;
		$option_name = self::launch_option_name( $matches[1] );
		$raw_payload = $wpdb->get_var( $wpdb->prepare( "SELECT option_value FROM {$wpdb->options} WHERE option_name = %s", $option_name ) );
		if ( ! is_string( $raw_payload ) || '' === $raw_payload ) {
			return 0;
		}

		$payload = json_decode( $raw_payload, true );
		if ( ! is_array( $payload ) ) {
			return 0;
		}

		$expected_secret_hash = isset( $payload['secret_hash'] ) ? (string) $payload['secret_hash'] : '';
		$expires_at           = isset( $payload['expires_at'] ) ? (int) $payload['expires_at'] : 0;
		$user_id              = isset( $payload['user_id'] ) ? (int) $payload['user_id'] : 0;
		if ( $expires_at < time() || $user_id <= 0 || ! hash_equals( $expected_secret_hash, hash( 'sha256', $matches[2] ) ) ) {
			return 0;
		}

		$deleted = $wpdb->query(
			$wpdb->prepare(
				"DELETE FROM {$wpdb->options} WHERE option_name = %s AND option_value = %s",
				$option_name,
				$raw_payload
			)
		);
		return 1 === (int) $deleted ? $user_id : 0;
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
