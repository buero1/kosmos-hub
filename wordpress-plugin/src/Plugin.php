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
	const REGISTER_RETRY_DELAY    = 300;
	const REGISTER_RETRY_THROTTLE = 900;

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
		add_filter( 'pre_set_site_transient_update_plugins', array( AbilityRegistry::class, 'capture_plugin_update_offers' ), PHP_INT_MAX );
		add_action( 'init', array( self::class, 'maybe_retry_registration' ) );
		add_action( 'wp_abilities_api_categories_init', array( AbilityRegistry::class, 'register_categories' ) );
		add_action( 'wp_abilities_api_init', array( AbilityRegistry::class, 'register_abilities' ) );
		add_action( self::REGISTER_HOOK, array( self::class, 'run_registration' ) );
		add_action( self::HEARTBEAT_HOOK, array( self::class, 'run_heartbeat' ) );
		add_action( 'admin_menu', array( StatusPage::class, 'register' ) );
		add_action( 'admin_post_kosmos_bridge_retry_registration', array( self::class, 'retry_registration' ) );
		add_action( 'admin_post_kosmos_bridge_save_settings', array( self::class, 'save_settings' ) );
		add_action( 'wp_ajax_kosmos_bridge_collect_update_inventory', array( AbilityRegistry::class, 'handle_admin_update_loopback' ) );
		add_action( 'wp_ajax_nopriv_kosmos_bridge_collect_update_inventory', array( AbilityRegistry::class, 'handle_admin_update_loopback' ) );
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

		self::schedule_registration_retry( time() + MINUTE_IN_SECONDS );

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
		$success = self::registrar()->register( false );

		if ( $success ) {
			wp_clear_scheduled_hook( self::REGISTER_HOOK );
			return true;
		}

		self::schedule_registration_retry();
		return false;
	}

	/**
	 * @return void
	 */
	public static function run_heartbeat() {
		$success = self::registrar()->register( true );

		if ( ! $success ) {
			self::schedule_registration_retry();
		}
	}

	/**
	 * @return void
	 */
	public static function maybe_retry_registration() {
		if ( '' !== Options::get_last_success_at() ) {
			return;
		}

		if ( wp_doing_cron() || wp_doing_ajax() ) {
			return;
		}

		if ( defined( 'REST_REQUEST' ) && REST_REQUEST ) {
			return;
		}

		if ( ! self::should_attempt_registration_now() ) {
			return;
		}

		self::run_registration();
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

	/**
	 * @param int|null $timestamp Unix timestamp for the retry.
	 * @return void
	 */
	private static function schedule_registration_retry( $timestamp = null ) {
		$next_scheduled = wp_next_scheduled( self::REGISTER_HOOK );
		$target_time    = is_int( $timestamp ) ? $timestamp : time() + self::REGISTER_RETRY_DELAY;

		if ( false !== $next_scheduled && $next_scheduled <= $target_time ) {
			return;
		}

		wp_schedule_single_event( $target_time, self::REGISTER_HOOK );
	}

	/**
	 * @return bool
	 */
	private static function should_attempt_registration_now() {
		$last_attempt = Options::get_last_registered_at();

		if ( '' === $last_attempt ) {
			return true;
		}

		$last_attempt_ts = strtotime( $last_attempt );
		if ( false === $last_attempt_ts ) {
			return true;
		}

		return ( time() - $last_attempt_ts ) >= self::REGISTER_RETRY_THROTTLE;
	}
}
