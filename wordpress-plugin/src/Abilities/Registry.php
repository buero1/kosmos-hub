<?php
namespace KosmosBridge\Abilities;

defined( 'ABSPATH' ) || exit;

class Registry {
	const UPDATE_LOOPBACK_ACTION = 'kosmos_bridge_collect_update_inventory';
	const UPDATE_LOOPBACK_TTL    = 60;
	const UPDATE_OFFER_CACHE_OPTION = 'kosmos_bridge_plugin_update_offers_v1';
	const UPDATE_OFFER_CACHE_TTL    = 172800;

	/**
	 * @return void
	 */
	public static function register_categories() {
		if ( ! function_exists( 'wp_register_ability_category' ) ) {
			return;
		}

		wp_register_ability_category(
			'kosmos-bridge',
			array(
				'label'       => __( 'Kosmos Bridge', 'kosmos-bridge' ),
				'description' => __( 'Read-only abilities exposed for Kosmos Hub discovery and diagnostics.', 'kosmos-bridge' ),
			)
		);
	}

	/**
	 * @return void
	 */
	public static function register_abilities() {
		if ( ! function_exists( 'wp_register_ability' ) ) {
			return;
		}

		wp_register_ability(
			'kosmos-bridge/get-site-info',
			array(
				'label'               => __( 'Get Site Info', 'kosmos-bridge' ),
				'description'         => __( 'Returns basic information about this WordPress site.', 'kosmos-bridge' ),
				'category'            => 'kosmos-bridge',
				'output_schema'       => self::site_info_output_schema(),
				'execute_callback'    => array( self::class, 'execute_get_site_info' ),
				'permission_callback' => array( self::class, 'allow_readonly_access' ),
				'meta'                => self::readonly_meta(),
			)
		);

		wp_register_ability(
			'kosmos-bridge/get-environment-info',
			array(
				'label'               => __( 'Get Environment Info', 'kosmos-bridge' ),
				'description'         => __( 'Returns software versions and runtime information for this WordPress site.', 'kosmos-bridge' ),
				'category'            => 'kosmos-bridge',
				'output_schema'       => self::environment_output_schema(),
				'execute_callback'    => array( self::class, 'execute_get_environment_info' ),
				'permission_callback' => array( self::class, 'allow_readonly_access' ),
				'meta'                => self::readonly_meta(),
			)
		);

		wp_register_ability(
			'kosmos-bridge/list-active-plugins',
			array(
				'label'               => __( 'List Active Plugins', 'kosmos-bridge' ),
				'description'         => __( 'Returns active plugin metadata for this WordPress site.', 'kosmos-bridge' ),
				'category'            => 'kosmos-bridge',
				'output_schema'       => self::active_plugins_output_schema(),
				'execute_callback'    => array( self::class, 'execute_list_active_plugins' ),
				'permission_callback' => array( self::class, 'allow_readonly_access' ),
				'meta'                => self::readonly_meta(),
			)
		);

		wp_register_ability(
			'kosmos-bridge/get-available-updates',
			array(
				'label'               => __( 'Get Available Updates', 'kosmos-bridge' ),
				'description'         => __( 'Checks and returns currently available WordPress, plugin, and theme updates for this site.', 'kosmos-bridge' ),
				'category'            => 'kosmos-bridge',
				'output_schema'       => self::available_updates_output_schema(),
				'execute_callback'    => array( self::class, 'execute_get_available_updates' ),
				'permission_callback' => array( self::class, 'allow_readonly_access' ),
				'meta'                => self::readonly_meta(),
			)
		);

		wp_register_ability(
			'kosmos-bridge/get-updraftplus-backup-status',
			array(
				'label'               => __( 'Get UpdraftPlus Backup Status', 'kosmos-bridge' ),
				'description'         => __( 'Returns read-only metadata about the latest complete UpdraftPlus backup.', 'kosmos-bridge' ),
				'category'            => 'kosmos-bridge',
				'output_schema'       => self::updraftplus_backup_status_output_schema(),
				'execute_callback'    => array( self::class, 'execute_get_updraftplus_backup_status' ),
				'permission_callback' => array( self::class, 'allow_readonly_access' ),
				'meta'                => self::readonly_meta(),
			)
		);
	}

	/**
	 * @return bool
	 */
	public static function allow_readonly_access() {
		return true;
	}

	/**
	 * @return array
	 */
	public static function execute_get_site_info() {
		return array(
			'name'         => get_bloginfo( 'name' ),
			'description'  => get_bloginfo( 'description' ),
			'home_url'     => home_url( '/' ),
			'site_url'     => site_url( '/' ),
			'language'     => get_bloginfo( 'language' ),
			'timezone'     => wp_timezone_string(),
			'is_multisite' => is_multisite(),
		);
	}

	/**
	 * @return array
	 */
	public static function execute_get_environment_info() {
		global $wp_version;

		return array(
			'wordpress_version' => is_string( $wp_version ) ? $wp_version : get_bloginfo( 'version' ),
			'php_version'       => PHP_VERSION,
			'bridge_version'    => \KosmosBridge\Options::get_bridge_version(),
			'abilities_api'     => function_exists( 'wp_register_ability' ),
			'site_uuid'         => \KosmosBridge\Options::get_site_uuid(),
		);
	}

	/**
	 * @return array
	 */
	public static function execute_list_active_plugins() {
		if ( ! function_exists( 'get_plugins' ) ) {
			require_once ABSPATH . 'wp-admin/includes/plugin.php';
		}

		$plugins        = get_plugins();
		$active_plugins = array_values( (array) get_option( 'active_plugins', array() ) );
		$items          = array();

		foreach ( $active_plugins as $plugin_file ) {
			$data    = isset( $plugins[ $plugin_file ] ) ? $plugins[ $plugin_file ] : array();
			$items[] = array(
				'plugin_file' => $plugin_file,
				'name'        => isset( $data['Name'] ) ? (string) $data['Name'] : $plugin_file,
				'version'     => isset( $data['Version'] ) ? (string) $data['Version'] : '',
				'author'      => isset( $data['AuthorName'] ) ? (string) $data['AuthorName'] : '',
				'plugin_uri'  => isset( $data['PluginURI'] ) ? (string) $data['PluginURI'] : '',
			);
		}

		return array(
			'count'   => count( $items ),
			'plugins' => $items,
		);
	}

	/**
	 * Read UpdraftPlus backup metadata without starting, downloading, restoring,
	 * or exposing backup files. The provider resolves a full backup against the
	 * backup entities configured on the individual site.
	 *
	 * @return array
	 */
	public static function execute_get_updraftplus_backup_status() {
		$plugin_file = 'updraftplus/updraftplus.php';
		$installed   = file_exists( WP_PLUGIN_DIR . '/' . $plugin_file );
		$active      = in_array( $plugin_file, (array) get_option( 'active_plugins', array() ), true );

		if ( is_multisite() ) {
			$network_active = (array) get_site_option( 'active_sitewide_plugins', array() );
			$active         = $active || isset( $network_active[ $plugin_file ] );
		}

		$result = array(
			'reported_at'      => gmdate( 'c' ),
			'provider'         => 'updraftplus',
			'installed'        => $installed,
			'active'           => $active,
			'available'        => false,
			'complete'         => false,
			'latest_backup_at' => '',
			'backup_count'     => 0,
			'components'       => array(),
			'message'          => '',
		);

		if ( ! $installed ) {
			$result['message'] = 'UpdraftPlus is not installed on this site.';
			return $result;
		}

		if ( ! $active || ! class_exists( 'UpdraftPlus_Backup_History', false ) ) {
			$result['message'] = 'UpdraftPlus is installed but not active or not initialized.';
			return $result;
		}

		try {
			$history = \UpdraftPlus_Backup_History::get_history();
			$history = is_array( $history ) ? $history : array();
			$nonce   = (string) \UpdraftPlus_Backup_History::get_latest_full_backup();
			$backup  = self::find_updraftplus_backup_by_nonce( $history, $nonce );
		} catch ( \Throwable $exception ) {
			$result['message'] = 'UpdraftPlus backup history could not be read.';
			return $result;
		}

		$result['backup_count'] = count( $history );
		if ( empty( $backup ) ) {
			$result['message'] = 'No complete UpdraftPlus backup is currently recorded.';
			return $result;
		}

		$timestamp = self::get_updraftplus_backup_timestamp( $backup );
		if ( $timestamp > 0 ) {
			$result['latest_backup_at'] = gmdate( 'c', $timestamp );
		}

		$result['available']  = $timestamp > 0;
		$result['complete']   = true;
		$result['components'] = self::get_updraftplus_backup_components( $backup );
		$result['message']    = $result['available'] ? 'Complete UpdraftPlus backup found.' : 'A complete backup was found without a usable timestamp.';

		return $result;
	}

	/**
	 * Refresh and read WordPress update transients. This intentionally never
	 * downloads or installs updates on the customer site.
	 *
	 * @return array
	 */
	public static function execute_get_available_updates() {
		if ( is_admin() ) {
			return self::collect_available_updates( 'admin' );
		}

		$admin_payload = self::request_admin_update_inventory();
		if ( is_array( $admin_payload ) ) {
			return $admin_payload;
		}

		return self::collect_available_updates( 'standard_fallback' );
	}

	/**
	 * Remember offers contributed through WordPress' shared plugin updater
	 * transient. Vendor updaters often contribute only during normal admin
	 * requests, while a later Hub refresh may only receive WordPress.org data.
	 *
	 * @param mixed $transient Plugin update transient before WordPress stores it.
	 * @return mixed
	 */
	public static function capture_plugin_update_offers( $transient ) {
		$transient_data = self::to_array( $transient );
		$responses      = isset( $transient_data['response'] ) ? self::to_array( $transient_data['response'] ) : array();
		$cached_entries = self::get_cached_plugin_update_entries();
		$now            = time();
		$changed        = false;

		foreach ( $responses as $plugin_file => $offer ) {
			if ( '' === self::get_update_version( self::to_array( $offer ) ) ) {
				continue;
			}

			$cached_entries[ (string) $plugin_file ] = array(
				'captured_at' => $now,
				'offer'       => $offer,
			);
			$changed = true;
		}

		if ( $changed ) {
			update_option( self::UPDATE_OFFER_CACHE_OPTION, $cached_entries, false );
		}

		return $transient;
	}

	/**
	 * Handle the one-time, same-site admin loopback used for update providers
	 * that only register their offers during WordPress admin bootstrap.
	 *
	 * @return void
	 */
	public static function handle_admin_update_loopback() {
		$token = isset( $_POST['token'] ) ? (string) wp_unslash( $_POST['token'] ) : '';
		if ( ! self::consume_loopback_token( $token ) ) {
			wp_send_json_error( array( 'code' => 'kosmos_bridge_loopback_forbidden' ), 403 );
		}

		wp_send_json_success( self::collect_available_updates( 'admin_loopback' ) );
	}

	/**
	 * Read the WordPress update state without installing or downloading updates.
	 *
	 * @param string $check_mode Source context for this update inventory.
	 * @return array
	 */
	private static function collect_available_updates( $check_mode ) {
		global $wp_version;

		if ( ! function_exists( 'get_plugins' ) ) {
			require_once ABSPATH . 'wp-admin/includes/plugin.php';
		}
		if ( ! function_exists( 'get_plugin_updates' ) ) {
			require_once ABSPATH . 'wp-admin/includes/update.php';
		}

		$current_wordpress_version = is_string( $wp_version ) ? $wp_version : get_bloginfo( 'version' );
		$plugins                   = get_plugins();
		self::refresh_update_transients( $plugins );
		$wordpress_updates          = array();
		$plugin_updates             = array();
		$theme_updates              = array();
		$preferred_core_update      = null;
		$site_locale                = function_exists( 'get_locale' ) ? get_locale() : '';

		$core_transient = self::to_array( get_site_transient( 'update_core' ) );
		$core_offers    = isset( $core_transient['updates'] ) ? (array) $core_transient['updates'] : array();
		foreach ( $core_offers as $update ) {
			$data    = self::to_array( $update );
			$version = isset( $data['version'] ) ? (string) $data['version'] : '';

			if ( '' === $version || version_compare( $version, $current_wordpress_version, '<=' ) ) {
				continue;
			}

			$candidate = array(
				'current_version' => $current_wordpress_version,
				'new_version'     => $version,
				'locale'          => isset( $data['locale'] ) ? (string) $data['locale'] : '',
			);

			if ( self::is_preferred_core_update( $candidate, $preferred_core_update, $site_locale ) ) {
				$preferred_core_update = $candidate;
			}
		}

		if ( null !== $preferred_core_update ) {
			$wordpress_updates[] = $preferred_core_update;
		}

		$native_plugin_updates = get_plugin_updates();
		foreach ( $native_plugin_updates as $plugin_file => $plugin ) {
			$plugin_record = self::to_array( $plugin );
			$data          = isset( $plugin_record['update'] ) ? self::to_array( $plugin_record['update'] ) : array();
			$resolved_file = isset( $data['plugin'] ) ? (string) $data['plugin'] : (string) $plugin_file;
			$plugin_data   = isset( $plugins[ $resolved_file ] ) ? $plugins[ $resolved_file ] : $plugin_record;
			$new_version   = self::get_update_version( $data );

			$plugin_updates[] = array(
				'plugin_file'     => $resolved_file,
				'name'            => isset( $plugin_data['Name'] ) ? (string) $plugin_data['Name'] : $resolved_file,
				'current_version' => isset( $plugin_data['Version'] ) ? (string) $plugin_data['Version'] : '',
				'new_version'     => $new_version,
			);
		}

		$theme_transient = self::to_array( get_site_transient( 'update_themes' ) );
		$theme_responses = isset( $theme_transient['response'] ) ? self::to_array( $theme_transient['response'] ) : array();
		foreach ( $theme_responses as $stylesheet => $update ) {
			$data        = self::to_array( $update );
			$new_version = isset( $data['new_version'] ) ? (string) $data['new_version'] : '';

			if ( '' === $new_version ) {
				continue;
			}

			$theme = wp_get_theme( (string) $stylesheet );
			$theme_updates[] = array(
				'stylesheet'      => (string) $stylesheet,
				'name'            => $theme->exists() ? (string) $theme->get( 'Name' ) : (string) $stylesheet,
				'current_version' => $theme->exists() ? (string) $theme->get( 'Version' ) : '',
				'new_version'     => $new_version,
			);
		}

		return array(
			'reported_at' => gmdate( 'c' ),
			'check_mode'  => $check_mode,
			'wordpress'   => array( 'updates' => $wordpress_updates ),
			'plugins'     => array( 'updates' => $plugin_updates ),
			'themes'      => array( 'updates' => $theme_updates ),
			'summary'     => array(
				'wordpress' => count( $wordpress_updates ),
				'plugins'   => count( $plugin_updates ),
				'themes'    => count( $theme_updates ),
				'total'     => count( $wordpress_updates ) + count( $plugin_updates ) + count( $theme_updates ),
			),
		);
	}

	/**
	 * Provide the read-only core abilities on WordPress versions without the
	 * native Abilities API so older managed sites use the same MCP contract.
	 *
	 * @return array
	 */
	public static function get_fallback_abilities() {
		return array(
			array(
				'name'          => 'kosmos-bridge/get-site-info',
				'label'         => __( 'Get Site Info', 'kosmos-bridge' ),
				'description'   => __( 'Returns basic information about this WordPress site.', 'kosmos-bridge' ),
				'category'      => 'kosmos-bridge',
				'input_schema'  => array(),
				'output_schema' => self::site_info_output_schema(),
				'meta'          => self::readonly_meta(),
			),
			array(
				'name'          => 'kosmos-bridge/get-environment-info',
				'label'         => __( 'Get Environment Info', 'kosmos-bridge' ),
				'description'   => __( 'Returns software versions and runtime information for this WordPress site.', 'kosmos-bridge' ),
				'category'      => 'kosmos-bridge',
				'input_schema'  => array(),
				'output_schema' => self::environment_output_schema(),
				'meta'          => self::readonly_meta(),
			),
			array(
				'name'          => 'kosmos-bridge/list-active-plugins',
				'label'         => __( 'List Active Plugins', 'kosmos-bridge' ),
				'description'   => __( 'Returns active plugin metadata for this WordPress site.', 'kosmos-bridge' ),
				'category'      => 'kosmos-bridge',
				'input_schema'  => array(),
				'output_schema' => self::active_plugins_output_schema(),
				'meta'          => self::readonly_meta(),
			),
			array(
				'name'          => 'kosmos-bridge/get-available-updates',
				'label'         => __( 'Get Available Updates', 'kosmos-bridge' ),
				'description'   => __( 'Checks and returns currently available WordPress, plugin, and theme updates for this site.', 'kosmos-bridge' ),
				'category'      => 'kosmos-bridge',
				'input_schema'  => array(),
				'output_schema' => self::available_updates_output_schema(),
				'meta'          => self::readonly_meta(),
			),
			array(
				'name'          => 'kosmos-bridge/get-updraftplus-backup-status',
				'label'         => __( 'Get UpdraftPlus Backup Status', 'kosmos-bridge' ),
				'description'   => __( 'Returns read-only metadata about the latest complete UpdraftPlus backup.', 'kosmos-bridge' ),
				'category'      => 'kosmos-bridge',
				'input_schema'  => array(),
				'output_schema' => self::updraftplus_backup_status_output_schema(),
				'meta'          => self::readonly_meta(),
			),
		);
	}

	/**
	 * @param string $ability_name Ability identifier.
	 * @return array|null
	 */
	public static function get_fallback_ability( $ability_name ) {
		foreach ( self::get_fallback_abilities() as $ability ) {
			if ( $ability_name === $ability['name'] ) {
				return $ability;
			}
		}

		return null;
	}

	/**
	 * @param string $ability_name Ability identifier.
	 * @param mixed  $input Ability input.
	 * @return array|null
	 */
	public static function execute_fallback_ability( $ability_name, $input = null ) {
		if ( null !== $input && ! empty( $input ) ) {
			return null;
		}

		switch ( $ability_name ) {
			case 'kosmos-bridge/get-site-info':
				return self::execute_get_site_info();
			case 'kosmos-bridge/get-environment-info':
				return self::execute_get_environment_info();
			case 'kosmos-bridge/list-active-plugins':
				return self::execute_list_active_plugins();
			case 'kosmos-bridge/get-available-updates':
				return self::execute_get_available_updates();
			case 'kosmos-bridge/get-updraftplus-backup-status':
				return self::execute_get_updraftplus_backup_status();
		}

		return null;
	}

	/**
	 * @param object $ability Ability instance.
	 * @return bool
	 */
	public static function is_public_ability( $ability ) {
		if ( ! is_object( $ability ) || ! method_exists( $ability, 'get_meta_item' ) ) {
			return false;
		}

		return true === $ability->get_meta_item( 'public', false );
	}

	/**
	 * @param object $ability Ability instance.
	 * @return array
	 */
	public static function serialize_ability( $ability ) {
		return array(
			'name'          => $ability->get_name(),
			'label'         => $ability->get_label(),
			'description'   => $ability->get_description(),
			'category'      => $ability->get_category(),
			'input_schema'  => self::normalize_object( $ability->get_input_schema() ),
			'output_schema' => self::normalize_object( $ability->get_output_schema() ),
			'meta'          => self::normalize_object( $ability->get_meta() ),
		);
	}

	/**
	 * @param mixed $value Value returned by the Abilities API.
	 * @return array
	 */
	private static function normalize_object( $value ) {
		return is_array( $value ) ? $value : array();
	}

	/**
	 * @return array
	 */
	private static function readonly_meta() {
		return array(
			'public'       => true,
			'show_in_rest' => true,
			'annotations'  => array(
				'readonly'    => true,
				'destructive' => false,
				'idempotent'  => true,
			),
		);
	}

	/**
	 * @return array
	 */
	private static function site_info_output_schema() {
		return array(
			'type'       => 'object',
			'properties' => array(
				'name'         => array( 'type' => 'string' ),
				'description'  => array( 'type' => 'string' ),
				'home_url'     => array( 'type' => 'string', 'format' => 'uri' ),
				'site_url'     => array( 'type' => 'string', 'format' => 'uri' ),
				'language'     => array( 'type' => 'string' ),
				'timezone'     => array( 'type' => 'string' ),
				'is_multisite' => array( 'type' => 'boolean' ),
			),
			'required'   => array( 'name', 'home_url', 'site_url', 'language', 'timezone', 'is_multisite' ),
		);
	}

	/**
	 * @return array
	 */
	private static function environment_output_schema() {
		return array(
			'type'       => 'object',
			'properties' => array(
				'wordpress_version' => array( 'type' => 'string' ),
				'php_version'       => array( 'type' => 'string' ),
				'bridge_version'    => array( 'type' => 'string' ),
				'abilities_api'     => array( 'type' => 'boolean' ),
				'site_uuid'         => array( 'type' => 'string' ),
			),
			'required'   => array( 'wordpress_version', 'php_version', 'bridge_version', 'abilities_api', 'site_uuid' ),
		);
	}

	/**
	 * @return array
	 */
	private static function active_plugins_output_schema() {
		return array(
			'type'       => 'object',
			'properties' => array(
				'count'   => array( 'type' => 'integer' ),
				'plugins' => array(
					'type'  => 'array',
					'items' => array(
						'type'       => 'object',
						'properties' => array(
							'plugin_file' => array( 'type' => 'string' ),
							'name'        => array( 'type' => 'string' ),
							'version'     => array( 'type' => 'string' ),
							'author'      => array( 'type' => 'string' ),
							'plugin_uri'  => array( 'type' => 'string' ),
						),
						'required'   => array( 'plugin_file', 'name', 'version', 'author', 'plugin_uri' ),
					),
				),
			),
			'required'   => array( 'count', 'plugins' ),
		);
	}

	/**
	 * @return array
	 */
	private static function available_updates_output_schema() {
		$update_item = array(
			'type'       => 'object',
			'properties' => array(
				'name'            => array( 'type' => 'string' ),
				'current_version' => array( 'type' => 'string' ),
				'new_version'     => array( 'type' => 'string' ),
			),
			'required'   => array( 'current_version', 'new_version' ),
		);

		return array(
			'type'       => 'object',
			'properties' => array(
				'reported_at' => array( 'type' => 'string', 'format' => 'date-time' ),
				'check_mode'  => array( 'type' => 'string' ),
				'wordpress'   => array( 'type' => 'object' ),
				'plugins'     => array(
					'type'       => 'object',
					'properties' => array( 'updates' => array( 'type' => 'array', 'items' => $update_item ) ),
				),
				'themes'      => array(
					'type'       => 'object',
					'properties' => array( 'updates' => array( 'type' => 'array', 'items' => $update_item ) ),
				),
				'summary'     => array( 'type' => 'object' ),
			),
			'required'   => array( 'reported_at', 'check_mode', 'wordpress', 'plugins', 'themes', 'summary' ),
		);
	}

	/**
	 * @return array
	 */
	private static function updraftplus_backup_status_output_schema() {
		return array(
			'type'       => 'object',
			'properties' => array(
				'reported_at'      => array( 'type' => 'string', 'format' => 'date-time' ),
				'provider'         => array( 'type' => 'string' ),
				'installed'        => array( 'type' => 'boolean' ),
				'active'           => array( 'type' => 'boolean' ),
				'available'        => array( 'type' => 'boolean' ),
				'complete'         => array( 'type' => 'boolean' ),
				// Empty when UpdraftPlus has no complete backup yet; the Hub parses a timestamp only when present.
				'latest_backup_at' => array( 'type' => 'string' ),
				'backup_count'     => array( 'type' => 'integer' ),
				'components'       => array( 'type' => 'array', 'items' => array( 'type' => 'string' ) ),
				'message'          => array( 'type' => 'string' ),
			),
			'required'   => array(
				'reported_at',
				'provider',
				'installed',
				'active',
				'available',
				'complete',
				'latest_backup_at',
				'backup_count',
				'components',
				'message',
			),
		);
	}

	/**
	 * @param array  $history UpdraftPlus backup history.
	 * @param string $nonce Backup nonce selected by UpdraftPlus.
	 * @return array
	 */
	private static function find_updraftplus_backup_by_nonce( $history, $nonce ) {
		if ( '' === $nonce ) {
			return array();
		}

		foreach ( $history as $backup_time => $backup ) {
			if ( ! is_array( $backup ) || ! isset( $backup['nonce'] ) || $nonce !== (string) $backup['nonce'] ) {
				continue;
			}

			$backup['_kosmos_backup_time'] = $backup_time;
			return $backup;
		}

		return array();
	}

	/**
	 * @param array $backup UpdraftPlus backup history entry.
	 * @return int
	 */
	private static function get_updraftplus_backup_timestamp( $backup ) {
		foreach ( array( '_kosmos_backup_time', 'backup_time', 'nonincremental_backup_time' ) as $key ) {
			if ( isset( $backup[ $key ] ) && is_numeric( $backup[ $key ] ) && (int) $backup[ $key ] > 0 ) {
				return (int) $backup[ $key ];
			}
		}

		return 0;
	}

	/**
	 * @param array $backup UpdraftPlus backup history entry.
	 * @return array
	 */
	private static function get_updraftplus_backup_components( $backup ) {
		$components = array();
		$entities   = array(
			'db'      => 'database',
			'plugins' => 'plugins',
			'themes'  => 'themes',
			'uploads' => 'uploads',
			'others'  => 'others',
		);

		foreach ( $entities as $prefix => $label ) {
			foreach ( array_keys( $backup ) as $backup_key ) {
				if ( 0 === strpos( (string) $backup_key, $prefix ) ) {
					$components[] = $label;
					break;
				}
			}
		}

		return $components;
	}

	/**
	 * WordPress reports historical and language fallback packages alongside the
	 * recommended core offer. The fleet only needs the one best next upgrade.
	 *
	 * @param array      $candidate Candidate core update.
	 * @param array|null $current Current preferred core update.
	 * @param string     $site_locale Site locale.
	 * @return bool
	 */
	private static function is_preferred_core_update( $candidate, $current, $site_locale ) {
		if ( null === $current ) {
			return true;
		}

		$candidate_is_local = '' !== $site_locale && $candidate['locale'] === $site_locale;
		$current_is_local   = '' !== $site_locale && $current['locale'] === $site_locale;

		if ( $candidate_is_local !== $current_is_local ) {
			return $candidate_is_local;
		}

		return version_compare( $candidate['new_version'], $current['new_version'], '>' );
	}

	/**
	 * WordPress.org uses new_version, while some trusted third-party updaters
	 * use version for the same update offer.
	 *
	 * @param array $data Update response data.
	 * @return string
	 */
	private static function get_update_version( $data ) {
		foreach ( array( 'new_version', 'version' ) as $key ) {
			$version = isset( $data[ $key ] ) ? trim( (string) $data[ $key ] ) : '';
			if ( '' !== $version ) {
				return $version;
			}
		}

		return '';
	}

	/**
	 * Run the same inventory inside admin-ajax so admin-only update providers
	 * can attach their standard WordPress update offers.
	 *
	 * @return array|null
	 */
	private static function request_admin_update_inventory() {
		$token = self::generate_loopback_token();
		set_transient( self::loopback_token_key( $token ), hash( 'sha256', $token ), self::UPDATE_LOOPBACK_TTL );

		$response = wp_remote_post(
			admin_url( 'admin-ajax.php' ),
			array(
				'timeout'     => 30,
				'redirection' => 0,
				'body'        => array(
					'action' => self::UPDATE_LOOPBACK_ACTION,
					'token'  => $token,
				),
			)
		);
		delete_transient( self::loopback_token_key( $token ) );

		if ( is_wp_error( $response ) || 200 !== (int) wp_remote_retrieve_response_code( $response ) ) {
			return null;
		}

		$payload = json_decode( wp_remote_retrieve_body( $response ), true );
		if ( ! is_array( $payload ) || empty( $payload['success'] ) || ! isset( $payload['data'] ) || ! is_array( $payload['data'] ) ) {
			return null;
		}

		return $payload['data'];
	}

	/**
	 * @param string $token One-time loopback token.
	 * @return bool
	 */
	private static function consume_loopback_token( $token ) {
		if ( '' === $token ) {
			return false;
		}

		$key      = self::loopback_token_key( $token );
		$expected = get_transient( $key );
		delete_transient( $key );

		return is_string( $expected ) && hash_equals( $expected, hash( 'sha256', $token ) );
	}

	/**
	 * @return string
	 */
	private static function generate_loopback_token() {
		try {
			return bin2hex( random_bytes( 32 ) );
		} catch ( \Exception $exception ) {
			return wp_generate_password( 64, false, false );
		}
	}

	/**
	 * @param string $token One-time loopback token.
	 * @return string
	 */
	private static function loopback_token_key( $token ) {
		return 'kosmos_bridge_update_loopback_' . hash( 'sha256', $token );
	}

	/**
	 * Refresh official update sources without discarding offers that trusted
	 * third-party updaters already registered in the current WordPress cache.
	 *
	 * @param array $plugins Installed plugin metadata keyed by plugin file.
	 * @return void
	 */
	private static function refresh_update_transients( $plugins ) {
		if ( ! function_exists( 'wp_version_check' ) ) {
			require_once ABSPATH . WPINC . '/update.php';
		}

		$previous_plugin_updates = self::to_array( get_site_transient( 'update_plugins' ) );
		$previous_theme_updates  = self::to_array( get_site_transient( 'update_themes' ) );

		delete_site_transient( 'update_core' );
		wp_version_check();
		wp_update_plugins();
		wp_update_themes();

		self::restore_missing_plugin_update_responses( $previous_plugin_updates, $plugins );
		self::restore_missing_plugin_update_responses(
			array( 'response' => self::get_cached_plugin_update_offers() ),
			$plugins,
			true
		);
		self::restore_missing_theme_update_responses( $previous_theme_updates );
	}

	/**
	 * @return array
	 */
	private static function get_cached_plugin_update_offers() {
		$offers = array();

		foreach ( self::get_cached_plugin_update_entries() as $plugin_file => $entry ) {
			if ( isset( $entry['offer'] ) ) {
				$offers[ $plugin_file ] = $entry['offer'];
			}
		}

		return $offers;
	}

	/**
	 * @return array
	 */
	private static function get_cached_plugin_update_entries() {
		$stored = get_option( self::UPDATE_OFFER_CACHE_OPTION, array() );
		$stored = is_array( $stored ) ? $stored : array();
		$now    = time();
		$valid  = array();

		foreach ( $stored as $plugin_file => $entry ) {
			if ( ! is_array( $entry ) ) {
				continue;
			}

			$captured_at = isset( $entry['captured_at'] ) ? (int) $entry['captured_at'] : 0;
			if ( $captured_at < ( $now - self::UPDATE_OFFER_CACHE_TTL ) || ! isset( $entry['offer'] ) ) {
				continue;
			}

			$valid[ (string) $plugin_file ] = $entry;
		}

		return $valid;
	}

	/**
	 * Keep valid offers from premium and vendor updaters when a core refresh did
	 * not put them back into the shared update transient.
	 *
	 * @param array $previous_transient Update transient before the refresh.
	 * @param array $plugins Installed plugin metadata keyed by plugin file.
	 * @param bool  $prefer_cached_offer Whether a valid vendor cache may take
	 *                                   precedence over WordPress.org no-update data.
	 * @return void
	 */
	private static function restore_missing_plugin_update_responses( $previous_transient, $plugins, $prefer_cached_offer = false ) {
		$previous_responses = isset( $previous_transient['response'] ) ? self::to_array( $previous_transient['response'] ) : array();
		$current            = get_site_transient( 'update_plugins' );
		$current_data       = self::to_array( $current );
		$current_responses  = isset( $current_data['response'] ) ? self::to_array( $current_data['response'] ) : array();
		$current_no_update  = isset( $current_data['no_update'] ) ? self::to_array( $current_data['no_update'] ) : array();
		$changed            = false;

		if ( empty( $previous_responses ) || empty( $current_data ) ) {
			return;
		}

		foreach ( $previous_responses as $plugin_file => $update ) {
			$update_data   = self::to_array( $update );
			$resolved_file = isset( $update_data['plugin'] ) ? (string) $update_data['plugin'] : (string) $plugin_file;
			$plugin_data   = isset( $plugins[ $resolved_file ] ) && is_array( $plugins[ $resolved_file ] ) ? $plugins[ $resolved_file ] : array();
			$new_version   = self::get_update_version( $update_data );
			$installed     = isset( $plugin_data['Version'] ) ? (string) $plugin_data['Version'] : '';

			if (
				'' === $resolved_file ||
				'' === $new_version ||
				'' === $installed ||
				! version_compare( $new_version, $installed, '>' ) ||
				array_key_exists( $resolved_file, $current_responses ) ||
				( ! $prefer_cached_offer && array_key_exists( $resolved_file, $current_no_update ) )
			) {
				continue;
			}

			$current_responses[ $resolved_file ] = $update;
			$changed                              = true;
		}

		if ( $changed ) {
			self::store_update_responses( 'update_plugins', $current, $current_data, $current_responses );
		}
	}

	/**
	 * Apply the same protection to premium themes that expose their offers
	 * through WordPress' standard update transient.
	 *
	 * @param array $previous_transient Update transient before the refresh.
	 * @return void
	 */
	private static function restore_missing_theme_update_responses( $previous_transient ) {
		$previous_responses = isset( $previous_transient['response'] ) ? self::to_array( $previous_transient['response'] ) : array();
		$current            = get_site_transient( 'update_themes' );
		$current_data       = self::to_array( $current );
		$current_responses  = isset( $current_data['response'] ) ? self::to_array( $current_data['response'] ) : array();
		$current_no_update  = isset( $current_data['no_update'] ) ? self::to_array( $current_data['no_update'] ) : array();
		$changed            = false;

		if ( empty( $previous_responses ) || empty( $current_data ) || ! function_exists( 'wp_get_theme' ) ) {
			return;
		}

		foreach ( $previous_responses as $stylesheet => $update ) {
			$update_data = self::to_array( $update );
			$new_version = isset( $update_data['new_version'] ) ? (string) $update_data['new_version'] : '';
			$theme       = wp_get_theme( (string) $stylesheet );
			$installed   = $theme->exists() ? (string) $theme->get( 'Version' ) : '';

			if (
				'' === $new_version ||
				'' === $installed ||
				! version_compare( $new_version, $installed, '>' ) ||
				array_key_exists( $stylesheet, $current_responses ) ||
				array_key_exists( $stylesheet, $current_no_update )
			) {
				continue;
			}

			$current_responses[ $stylesheet ] = $update;
			$changed                           = true;
		}

		if ( $changed ) {
			self::store_update_responses( 'update_themes', $current, $current_data, $current_responses );
		}
	}

	/**
	 * @param string $transient_name Update transient name.
	 * @param mixed  $current Raw transient value.
	 * @param array  $current_data Normalized transient value.
	 * @param array  $responses Merged update responses.
	 * @return void
	 */
	private static function store_update_responses( $transient_name, $current, $current_data, $responses ) {
		if ( is_object( $current ) ) {
			$current->response = $responses;
		} else {
			$current             = $current_data;
			$current['response'] = $responses;
		}

		set_site_transient( $transient_name, $current );
	}

	/**
	 * Normalize WordPress transients, which can be arrays or stdClass values.
	 *
	 * @param mixed $value Value to normalize.
	 * @return array
	 */
	private static function to_array( $value ) {
		if ( is_array( $value ) ) {
			return $value;
		}

		return is_object( $value ) ? get_object_vars( $value ) : array();
	}
}
