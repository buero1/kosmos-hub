<?php
namespace KosmosBridge;

use KosmosBridge\Abilities\Registry as AbilityRegistry;
use KosmosBridge\Admin\StatusPage;
use KosmosBridge\Registration\Registrar;
use KosmosBridge\Registration\SecretStore;
use KosmosBridge\Rest\DebugController;
use KosmosBridge\Rest\McpController;
use KosmosBridge\Updates\PluginUpdater;

defined( 'ABSPATH' ) || exit;

class Plugin {
	const REGISTER_HOOK  = 'kosmos_bridge_register_site';
	const HEARTBEAT_HOOK = 'kosmos_bridge_daily_heartbeat';

	/**
	 * @var Registrar|null
	 */
	private static $registrar = null;

	/**
	 * @param string $plugin_file Plugin bootstrap file.
	 * @return void
	 */
	public static function boot( $plugin_file ) {
		register_activation_hook( $plugin_file, array( self::class, 'activate' ) );
		register_deactivation_hook( $plugin_file, array( self::class, 'deactivate' ) );

		add_action( 'plugins_loaded', array( self::class, 'ensure_identity' ) );
		add_action( 'plugins_loaded', array( PluginUpdater::class, 'boot' ) );
		add_action( 'wp_abilities_api_categories_init', array( AbilityRegistry::class, 'register_categories' ) );
		add_action( 'wp_abilities_api_init', array( AbilityRegistry::class, 'register_abilities' ) );
		add_action( self::REGISTER_HOOK, array( self::class, 'run_registration' ) );
		add_action( self::HEARTBEAT_HOOK, array( self::class, 'run_heartbeat' ) );
		add_action( 'admin_menu', array( StatusPage::class, 'register' ) );
		add_action( 'admin_post_kosmos_bridge_retry_registration', array( self::class, 'retry_registration' ) );
		add_action( 'admin_post_kosmos_bridge_save_settings', array( self::class, 'save_settings' ) );
		add_action( 'rest_api_init', array( DebugController::class, 'register_routes' ) );
		add_action( 'rest_api_init', array( McpController::class, 'register_routes' ) );
	}

	/**
	 * @return void
	 */
	public static function activate() {
		SecretStore::ensure_identity();

		if ( ! wp_next_scheduled( self::HEARTBEAT_HOOK ) ) {
			wp_schedule_event( time() + HOUR_IN_SECONDS, 'daily', self::HEARTBEAT_HOOK );
		}

		if ( ! wp_next_scheduled( self::REGISTER_HOOK ) ) {
			wp_schedule_single_event( time() + MINUTE_IN_SECONDS, self::REGISTER_HOOK );
		}

		// Try the first registration immediately so mass activation needs no
		// follow-up click. The scheduled retry remains as a fallback.
		self::run_registration();
	}

	/**
	 * @return void
	 */
	public static function deactivate() {
		wp_clear_scheduled_hook( self::REGISTER_HOOK );
		wp_clear_scheduled_hook( self::HEARTBEAT_HOOK );
	}

	/**
	 * @return void
	 */
	public static function ensure_identity() {
		SecretStore::ensure_identity();
	}

	/**
	 * @return void
	 */
	public static function run_registration() {
		self::registrar()->register( false );
	}

	/**
	 * @return void
	 */
	public static function run_heartbeat() {
		self::registrar()->register( true );
	}

	/**
	 * @return void
	 */
	public static function retry_registration() {
		if ( ! current_user_can( 'manage_options' ) ) {
			wp_die( esc_html__( 'You are not allowed to retry registration.', 'kosmos-bridge' ) );
		}

		check_admin_referer( 'kosmos_bridge_retry_registration' );
		self::run_registration();

		wp_safe_redirect( admin_url( 'tools.php?page=kosmos-bridge&retried=1' ) );
		exit;
	}

	/**
	 * @return void
	 */
	public static function save_settings() {
		if ( ! current_user_can( 'manage_options' ) ) {
			wp_die( esc_html__( 'You are not allowed to update Kosmos Bridge settings.', 'kosmos-bridge' ) );
		}

		check_admin_referer( 'kosmos_bridge_save_settings' );

		$hub_url = isset( $_POST['kosmos_hub_base_url'] ) ? wp_unslash( $_POST['kosmos_hub_base_url'] ) : '';
		$hub_url = esc_url_raw( trim( (string) $hub_url ) );

		update_option( Options::SERVER_BASE_URL, untrailingslashit( $hub_url ), false );

		wp_safe_redirect( admin_url( 'tools.php?page=kosmos-bridge&saved=1' ) );
		exit;
	}

	/**
	 * @return Registrar
	 */
	private static function registrar() {
		if ( null === self::$registrar ) {
			self::$registrar = new Registrar();
		}

		return self::$registrar;
	}
}
