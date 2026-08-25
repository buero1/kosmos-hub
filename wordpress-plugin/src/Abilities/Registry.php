<?php
namespace KosmosBridge\Abilities;

defined( 'ABSPATH' ) || exit;

class Registry {
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
	 * Refresh and read WordPress update transients. This intentionally never
	 * downloads or installs updates on the customer site.
	 *
	 * @return array
	 */
	public static function execute_get_available_updates() {
		global $wp_version;

		if ( ! function_exists( 'get_plugins' ) ) {
			require_once ABSPATH . 'wp-admin/includes/plugin.php';
		}

		self::refresh_update_transients();

		$current_wordpress_version = is_string( $wp_version ) ? $wp_version : get_bloginfo( 'version' );
		$plugins                   = get_plugins();
		$wordpress_updates          = array();
		$plugin_updates             = array();
		$theme_updates              = array();

		foreach ( (array) get_site_transient( 'update_core' ) as $update ) {
			$data    = self::to_array( $update );
			$version = isset( $data['version'] ) ? (string) $data['version'] : '';

			if ( '' === $version || version_compare( $version, $current_wordpress_version, '<=' ) ) {
				continue;
			}

			$wordpress_updates[] = array(
				'current_version' => $current_wordpress_version,
				'new_version'     => $version,
				'locale'          => isset( $data['locale'] ) ? (string) $data['locale'] : '',
			);
		}

		$plugin_transient = self::to_array( get_site_transient( 'update_plugins' ) );
		$plugin_responses = isset( $plugin_transient['response'] ) ? self::to_array( $plugin_transient['response'] ) : array();
		foreach ( $plugin_responses as $plugin_file => $update ) {
			$data        = self::to_array( $update );
			$resolved_file = isset( $data['plugin'] ) ? (string) $data['plugin'] : (string) $plugin_file;
			$plugin_data = isset( $plugins[ $resolved_file ] ) ? $plugins[ $resolved_file ] : array();
			$new_version = isset( $data['new_version'] ) ? (string) $data['new_version'] : '';

			if ( '' === $new_version ) {
				continue;
			}

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
			'check_mode'  => 'fresh',
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
	 * Force WordPress to refresh its official update sources before returning a
	 * fleet snapshot. Only transient cache values are updated here.
	 *
	 * @return void
	 */
	private static function refresh_update_transients() {
		if ( ! function_exists( 'wp_version_check' ) ) {
			require_once ABSPATH . WPINC . '/update.php';
		}

		delete_site_transient( 'update_core' );
		delete_site_transient( 'update_plugins' );
		delete_site_transient( 'update_themes' );

		wp_version_check();
		wp_update_plugins();
		wp_update_themes();
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
