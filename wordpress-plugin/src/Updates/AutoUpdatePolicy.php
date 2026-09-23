<?php
namespace KosmosBridge\Updates;

defined( 'ABSPATH' ) || exit;

class AutoUpdatePolicy {
	const OPTION = 'kosmos_bridge_plugin_auto_update_blocks';
	const READ_ABILITY = 'kosmos-bridge/get-plugin-auto-update-policy';
	const WRITE_ABILITY = 'kosmos-bridge/set-plugin-auto-update-policy';

	public static function boot() {
		add_filter( 'auto_update_plugin', array( self::class, 'filter_update' ), PHP_INT_MAX, 2 );
		add_filter( 'pre_update_site_option_auto_update_plugins', array( self::class, 'filter_selection' ) );
		add_filter( 'plugin_auto_update_setting_html', array( self::class, 'setting_html' ), 10, 2 );
	}

	public static function blocks() {
		return (array) get_site_option( self::OPTION, array() );
	}

	public static function filter_update( $update, $item ) {
		return isset( $item->plugin ) && in_array( $item->plugin, self::blocks(), true ) ? false : $update;
	}

	public static function filter_selection( $plugins ) {
		return array_values( array_diff( (array) $plugins, self::blocks() ) );
	}

	public static function setting_html( $html, $plugin_file ) {
		return in_array( $plugin_file, self::blocks(), true )
			? esc_html__( 'Automatische Updates durch Kosmos Hub gesperrt.', 'kosmos-bridge' ) : $html;
	}

	public static function definitions() {
		$properties = array(
			'plugin_files' => array( 'type' => 'array', 'minItems' => 1, 'maxItems' => 100,
				'items' => array( 'type' => 'string', 'maxLength' => 255 ) ),
		);
		$definitions = array();
		foreach ( array( self::READ_ABILITY, self::WRITE_ABILITY ) as $name ) {
			$write = self::WRITE_ABILITY === $name;
			$input = $properties;
			if ( $write ) {
				$input['blocked'] = array( 'type' => 'boolean' );
			}
			$definitions[] = array(
				'name' => $name,
				'label' => $write ? 'Set plugin automatic update policy' : 'Read plugin automatic update policy',
				'description' => 'Controls selected WordPress background updates only. Releasing a block does not enable automatic updates. Manual, hosting and external management updates are unaffected.',
				'category' => 'kosmos-bridge',
				'input_schema' => array( 'type' => 'object', 'properties' => $input,
					'required' => array_keys( $input ), 'additionalProperties' => false ),
				'output_schema' => array( 'type' => 'object', 'properties' => array(
					'plugins' => array( 'type' => 'array', 'items' => array( 'type' => 'object', 'properties' => array(
						'plugin_file' => array( 'type' => 'string' ), 'installed' => array( 'type' => 'boolean' ),
						'blocked' => array( 'type' => 'boolean' ), 'configured' => array( 'type' => 'boolean' ),
						'verified' => array( 'type' => 'boolean' ),
					), 'required' => array( 'plugin_file', 'installed', 'blocked', 'configured', 'verified' ) ) ),
				), 'required' => array( 'plugins' ) ),
				'meta' => array( 'public' => true, 'show_in_rest' => true,
					'annotations' => array( 'readonly' => ! $write, 'destructive' => false, 'idempotent' => true ) ),
			);
		}
		return $definitions;
	}

	public static function read( $input ) {
		return self::execute( $input, false );
	}

	public static function write( $input ) {
		return self::execute( $input, true );
	}

	private static function execute( $input, $write ) {
		// Native abilities can be called outside the signed MCP endpoint too.
		if ( ! self::authorized() ) {
			return new \WP_Error( 'KOSMOS_BRIDGE_FORBIDDEN', 'Authenticated Hub or plugin administrator required.', array( 'status' => 403 ) );
		}
		if ( is_multisite() ) {
			return new \WP_Error( 'KOSMOS_BRIDGE_MULTISITE_UNSUPPORTED', 'Network-wide auto-update changes require a separate workflow.', array( 'status' => 409 ) );
		}
		if ( ! is_array( $input ) || ! isset( $input['plugin_files'] ) || ! is_array( $input['plugin_files'] )
			|| count( $input['plugin_files'] ) < 1 || count( $input['plugin_files'] ) > 100
			|| ( $write && ( ! isset( $input['blocked'] ) || ! is_bool( $input['blocked'] ) ) )
			|| array_diff( array_keys( $input ), $write ? array( 'plugin_files', 'blocked' ) : array( 'plugin_files' ) ) ) {
			return new \WP_Error( 'KOSMOS_BRIDGE_INVALID_INPUT', 'Explicit plugin files and a boolean policy are required.', array( 'status' => 400 ) );
		}
		foreach ( $input['plugin_files'] as $file ) {
			if ( ! is_string( $file ) || strlen( $file ) > 255 || ! preg_match( '~^[a-zA-Z0-9_.-]+(?:/[a-zA-Z0-9_.-]+)*\.php$~D', $file ) || false !== strpos( $file, '..' ) ) {
				return new \WP_Error( 'KOSMOS_BRIDGE_INVALID_INPUT', 'Invalid plugin file.', array( 'status' => 400 ) );
			}
		}
		if ( ! function_exists( 'get_plugins' ) ) {
			require_once ABSPATH . 'wp-admin/includes/plugin.php';
		}
		$plugins = get_plugins();
		$files = array_values( array_unique( $input['plugin_files'] ) );
		$blocks = self::blocks();
		if ( $write ) {
			foreach ( $files as $file ) {
				if ( ! isset( $plugins[ $file ] ) ) {
					continue;
				}
				$blocks = array_values( array_diff( $blocks, array( $file ) ) );
				if ( $input['blocked'] ) {
					$blocks[] = $file;
				}
			}
			update_site_option( self::OPTION, $blocks );
			if ( $input['blocked'] ) {
				update_site_option( 'auto_update_plugins', self::filter_selection( get_site_option( 'auto_update_plugins', array() ) ) );
			}
		}
		$blocks = self::blocks();
		$configured = (array) get_site_option( 'auto_update_plugins', array() );
		$rows = array();
		foreach ( $files as $file ) {
			$blocked = in_array( $file, $blocks, true );
			$enabled = in_array( $file, $configured, true );
			// Check the actual WordPress filter chain, not just our saved option.
			$verified = ! $write || ( $blocked === $input['blocked'] && ( ! $blocked || ( ! $enabled && false === apply_filters( 'auto_update_plugin', true, (object) array( 'plugin' => $file ) ) ) ) );
			$rows[] = array( 'plugin_file' => $file, 'installed' => isset( $plugins[ $file ] ),
				'blocked' => $blocked, 'configured' => $enabled, 'verified' => $verified );
		}
		return array( 'plugins' => $rows );
	}

	public static function authorized() {
		return \KosmosBridge\Security\SiteAuth::is_authenticated() || current_user_can( 'update_plugins' );
	}
}
