<?php
namespace KosmosBridge\Abilities;

defined( 'ABSPATH' ) || exit;

/** Bounded, revision-protected access to Yoast metadata for published pages. */
class SeoMetadata {
	const READ_ABILITY  = 'kosmos-bridge/read-page-seo';
	const WRITE_ABILITY = 'kosmos-bridge/write-page-seo';
	const MAX_PAGES     = 40;
	const MAX_CONTENT   = 6000;

	/**
	 * @return array
	 */
	public static function definitions() {
		$page_properties = array(
			'id'              => array( 'type' => 'integer', 'minimum' => 1 ),
			'page_title'      => array( 'type' => 'string' ),
			'slug'            => array( 'type' => 'string' ),
			'url'             => array( 'type' => 'string' ),
			'content'         => array( 'type' => 'string' ),
			'seo_title'       => array( 'type' => 'string' ),
			'seo_description' => array( 'type' => 'string' ),
			'indexable'       => array( 'type' => 'boolean' ),
			'is_front_page'   => array( 'type' => 'boolean' ),
			'revision'        => array( 'type' => 'string', 'pattern' => '^[a-f0-9]{64}$' ),
		);
		$output = array(
			'type'                 => 'object',
			'properties'           => array(
				'yoast_active'  => array( 'type' => 'boolean' ),
				'yoast_version' => array( 'type' => 'string' ),
				'site_name'     => array( 'type' => 'string' ),
				'language'      => array( 'type' => 'string' ),
				'pages'         => array(
					'type'     => 'array',
					'maxItems' => self::MAX_PAGES,
					'items'    => array(
						'type'                 => 'object',
						'properties'           => $page_properties,
						'required'             => array_keys( $page_properties ),
						'additionalProperties' => false,
					),
				),
			),
			'required'             => array( 'yoast_active', 'yoast_version', 'site_name', 'language', 'pages' ),
			'additionalProperties' => false,
		);
		$write_page = array(
			'type'                 => 'object',
			'properties'           => array(
				'id'          => array( 'type' => 'integer', 'minimum' => 1 ),
				'title'       => array( 'type' => 'string', 'maxLength' => 120 ),
				'description' => array( 'type' => 'string', 'maxLength' => 320 ),
				'revision'    => array( 'type' => 'string', 'pattern' => '^[a-f0-9]{64}$' ),
			),
			'required'             => array( 'id', 'title', 'description', 'revision' ),
			'additionalProperties' => false,
		);
		$write_input = array(
			'type'                 => 'object',
			'properties'           => array(
				'pages' => array( 'type' => 'array', 'minItems' => 1, 'maxItems' => self::MAX_PAGES, 'items' => $write_page ),
			),
			'required'             => array( 'pages' ),
			'additionalProperties' => false,
		);
		$write_output = array(
			'type'                 => 'object',
			'properties'           => array(
				'changed' => array( 'type' => 'integer', 'minimum' => 0 ),
				'pages'   => array( 'type' => 'array', 'maxItems' => self::MAX_PAGES, 'items' => $write_page ),
			),
			'required'             => array( 'changed', 'pages' ),
			'additionalProperties' => false,
		);
		$base_meta = array( 'public' => true, 'show_in_rest' => true );

		return array(
			array(
				'name'          => self::READ_ABILITY,
				'label'         => 'Read page SEO metadata',
				'description'   => 'Returns published WordPress pages, bounded visible text, and their stored Yoast title and description.',
				'category'      => 'kosmos-bridge',
				'input_schema'  => array( 'type' => 'object', 'properties' => array(), 'additionalProperties' => false ),
				'output_schema' => $output,
				'meta'          => $base_meta + array( 'annotations' => array( 'readonly' => true, 'destructive' => false, 'idempotent' => true ) ),
			),
			array(
				'name'          => self::WRITE_ABILITY,
				'label'         => 'Write page SEO metadata',
				'description'   => 'Writes explicitly confirmed Yoast titles and descriptions for unchanged published pages and verifies the stored values.',
				'category'      => 'kosmos-bridge',
				'input_schema'  => $write_input,
				'output_schema' => $write_output,
				'meta'          => $base_meta + array( 'annotations' => array( 'readonly' => false, 'destructive' => false, 'idempotent' => true ) ),
			),
		);
	}

	/**
	 * @return bool
	 */
	public static function authorized() {
		return \KosmosBridge\Security\SiteAuth::is_authenticated() || current_user_can( 'edit_pages' );
	}

	/**
	 * @param mixed $input Unused, validated for fallback WordPress versions.
	 * @return array|\WP_Error
	 */
	public static function read( $input = array() ) {
		if ( ! self::authorized() ) {
			return self::error( 'KOSMOS_BRIDGE_FORBIDDEN', 'Authenticated Hub or page editor required.', 403 );
		}
		if ( null !== $input && ! is_array( $input ) ) {
			return self::error( 'KOSMOS_BRIDGE_INVALID_INPUT', 'SEO read input must be an object.', 400 );
		}
		$posts = get_posts(
			array(
				'post_type'      => 'page',
				'post_status'    => 'publish',
				'numberposts'    => self::MAX_PAGES,
				'orderby'        => array( 'menu_order' => 'ASC', 'title' => 'ASC', 'ID' => 'ASC' ),
				'suppress_filters' => false,
			)
		);
		$front_page = (int) get_option( 'page_on_front', 0 );
		$pages      = array();
		foreach ( $posts as $post ) {
			$pages[] = self::page( $post, $front_page );
		}
		usort(
			$pages,
			static function ( $left, $right ) {
				return (int) $right['is_front_page'] <=> (int) $left['is_front_page'];
			}
		);
		return array(
			'yoast_active'  => self::yoast_active(),
			'yoast_version' => defined( 'WPSEO_VERSION' ) ? (string) WPSEO_VERSION : '',
			'site_name'     => (string) get_bloginfo( 'name' ),
			'language'      => (string) get_bloginfo( 'language' ),
			'pages'         => $pages,
		);
	}

	/**
	 * @param mixed $input Selected page changes.
	 * @return array|\WP_Error
	 */
	public static function write( $input ) {
		if ( ! self::authorized() ) {
			return self::error( 'KOSMOS_BRIDGE_FORBIDDEN', 'Authenticated Hub or page editor required.', 403 );
		}
		if ( ! self::yoast_active() ) {
			return self::error( 'KOSMOS_BRIDGE_YOAST_REQUIRED', 'Yoast SEO must be active before page SEO metadata can be changed.', 409 );
		}
		if ( ! is_array( $input ) || array( 'pages' ) !== array_keys( $input ) || ! is_array( $input['pages'] )
			|| count( $input['pages'] ) < 1 || count( $input['pages'] ) > self::MAX_PAGES ) {
			return self::error( 'KOSMOS_BRIDGE_INVALID_INPUT', 'One to forty explicit page changes are required.', 400 );
		}

		$validated = array();
		$seen      = array();
		foreach ( $input['pages'] as $change ) {
			$row = self::validated_change( $change );
			if ( is_wp_error( $row ) ) {
				return $row;
			}
			if ( isset( $seen[ $row['id'] ] ) ) {
				return self::error( 'KOSMOS_BRIDGE_INVALID_INPUT', 'Each page may be changed only once.', 400 );
			}
			$seen[ $row['id'] ] = true;
			$post               = get_post( $row['id'] );
			if ( ! $post || 'page' !== $post->post_type || 'publish' !== $post->post_status ) {
				return self::error( 'KOSMOS_BRIDGE_PAGE_UNAVAILABLE', 'A selected published page is no longer available.', 409 );
			}
			if ( ! hash_equals( self::revision( $post ), $row['revision'] ) ) {
				return self::error( 'KOSMOS_BRIDGE_SEO_CONFLICT', 'A selected page changed after the SEO preview was created.', 409 );
			}
			$validated[] = array( 'post' => $post, 'change' => $row );
		}

		$result    = array();
		$originals = array();
		foreach ( $validated as $item ) {
			$post   = $item['post'];
			$change = $item['change'];
			$originals[ $post->ID ] = array(
				'title'       => (string) get_post_meta( $post->ID, '_yoast_wpseo_title', true ),
				'description' => (string) get_post_meta( $post->ID, '_yoast_wpseo_metadesc', true ),
			);
			update_post_meta( $post->ID, '_yoast_wpseo_title', $change['title'] );
			update_post_meta( $post->ID, '_yoast_wpseo_metadesc', $change['description'] );
			clean_post_cache( $post->ID );
			$post = get_post( $post->ID );
			if ( (string) get_post_meta( $post->ID, '_yoast_wpseo_title', true ) !== $change['title']
				|| (string) get_post_meta( $post->ID, '_yoast_wpseo_metadesc', true ) !== $change['description'] ) {
				foreach ( $originals as $original_id => $original ) {
					update_post_meta( $original_id, '_yoast_wpseo_title', $original['title'] );
					update_post_meta( $original_id, '_yoast_wpseo_metadesc', $original['description'] );
					clean_post_cache( $original_id );
				}
				return self::error( 'KOSMOS_BRIDGE_SEO_WRITE_FAILED', 'WordPress did not confirm all selected SEO values.', 500 );
			}
			$result[] = array(
				'id'          => (int) $post->ID,
				'title'       => $change['title'],
				'description' => $change['description'],
				'revision'    => self::revision( $post ),
			);
		}
		return array( 'changed' => count( $result ), 'pages' => $result );
	}

	/**
	 * @param object $post WordPress page.
	 * @param int    $front_page Front-page ID.
	 * @return array
	 */
	private static function page( $post, $front_page ) {
		return array(
			'id'              => (int) $post->ID,
			'page_title'      => (string) get_the_title( $post ),
			'slug'            => (string) $post->post_name,
			'url'             => (string) get_permalink( $post ),
			'content'         => self::visible_text( $post ),
			'seo_title'       => (string) get_post_meta( $post->ID, '_yoast_wpseo_title', true ),
			'seo_description' => (string) get_post_meta( $post->ID, '_yoast_wpseo_metadesc', true ),
			'indexable'       => '1' !== (string) get_post_meta( $post->ID, '_yoast_wpseo_meta-robots-noindex', true ),
			'is_front_page'   => (int) $post->ID === (int) $front_page,
			'revision'        => self::revision( $post ),
		);
	}

	/**
	 * @param object $post WordPress page.
	 * @return string
	 */
	private static function visible_text( $post ) {
		$html = (string) $post->post_content;
		if ( function_exists( 'did_action' ) && did_action( 'elementor/loaded' ) && class_exists( '\\Elementor\\Plugin' ) ) {
			try {
				$rendered = \Elementor\Plugin::instance()->frontend->get_builder_content_for_display( $post->ID, true );
				if ( is_string( $rendered ) && '' !== trim( $rendered ) ) {
					$html = $rendered;
				}
			} catch ( \Throwable $error ) {
				// Fall back to the stored page content when a builder cannot render safely.
			}
		}
		if ( function_exists( 'strip_shortcodes' ) ) {
			$html = strip_shortcodes( $html );
		}
		$text = html_entity_decode( wp_strip_all_tags( $html, true ), ENT_QUOTES, get_bloginfo( 'charset' ) ?: 'UTF-8' );
		$text = preg_replace( '/\s+/u', ' ', $text );
		$text = is_string( $text ) ? trim( $text ) : '';
		return self::length( $text ) > self::MAX_CONTENT ? self::substring( $text, 0, self::MAX_CONTENT ) : $text;
	}

	/**
	 * @param mixed $change Candidate page mutation.
	 * @return array|\WP_Error
	 */
	private static function validated_change( $change ) {
		if ( ! is_array( $change ) || count( $change ) !== 4
			|| array_diff( array_keys( $change ), array( 'id', 'title', 'description', 'revision' ) )
			|| ! isset( $change['id'], $change['title'], $change['description'], $change['revision'] )
			|| ! is_int( $change['id'] ) || $change['id'] < 1 || ! is_string( $change['title'] )
			|| ! is_string( $change['description'] ) || ! is_string( $change['revision'] )
			|| ! preg_match( '/^[a-f0-9]{64}$/D', $change['revision'] ) ) {
			return self::error( 'KOSMOS_BRIDGE_INVALID_INPUT', 'Each SEO change needs an exact page ID, title, description, and revision.', 400 );
		}
		$title       = trim( preg_replace( '/\s+/u', ' ', $change['title'] ) );
		$description = trim( preg_replace( '/\s+/u', ' ', $change['description'] ) );
		if ( self::length( $title ) > 120 || self::length( $description ) > 320
			|| preg_match( '/[<>\x00-\x1f\x7f]/u', $title . $description ) ) {
			return self::error( 'KOSMOS_BRIDGE_INVALID_INPUT', 'SEO text contains markup, control characters, or exceeds the allowed length.', 400 );
		}
		return array( 'id' => $change['id'], 'title' => $title, 'description' => $description, 'revision' => $change['revision'] );
	}

	/**
	 * @param object $post WordPress page.
	 * @return string
	 */
	private static function revision( $post ) {
		return hash( 'sha256', implode( "\n", array(
			(string) $post->ID,
			(string) $post->post_modified_gmt,
			(string) get_post_meta( $post->ID, '_yoast_wpseo_title', true ),
			(string) get_post_meta( $post->ID, '_yoast_wpseo_metadesc', true ),
			(string) get_post_meta( $post->ID, '_yoast_wpseo_meta-robots-noindex', true ),
		) ) );
	}

	/**
	 * @return bool
	 */
	private static function yoast_active() {
		return defined( 'WPSEO_VERSION' ) || class_exists( 'WPSEO_Options' ) || class_exists( '\\Yoast\\WP\\SEO\\Main' );
	}

	private static function length( $value ) {
		return function_exists( 'mb_strlen' ) ? mb_strlen( $value, 'UTF-8' ) : strlen( $value );
	}

	private static function substring( $value, $start, $length ) {
		return function_exists( 'mb_substr' ) ? mb_substr( $value, $start, $length, 'UTF-8' ) : substr( $value, $start, $length );
	}

	private static function error( $code, $message, $status ) {
		return new \WP_Error( $code, $message, array( 'status' => $status ) );
	}
}
