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
}
